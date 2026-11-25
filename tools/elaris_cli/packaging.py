"""Assemble Elaris's app bundles: a macOS .app and a Linux prefix tree, each with Ladybird's engine and the brand icon."""

import importlib.util
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

from . import engine, ui
from .common import ENGINE, ROOT, VCPKG_INSTALLED, run, vcpkg_triplet

ASSETS = ROOT / "assets"
PACKAGING = ROOT / "packaging"
LINUX_ASSETS = PACKAGING / "linux"
# Compiled from the brand kit's Icon Composer documents, so the designer's layers and gradient carry over.
APP_ICONS = ASSETS / "brand-kit" / "apple" / "Icon Exports"
# Stable releases wear the blue icon; dev runs and canary builds the yellow one, so they are easy to tell apart.
# The app reads the channel from Info.plist and adds a DEV tag to its Dock icon in dev runs.
APP_ICON_SOURCES = {
    "stable": APP_ICONS / "Icon.icon",
    "dev": APP_ICONS / "Icon-Dev.icon",
    "canary": APP_ICONS / "Icon-Dev.icon",
}
# The flat bitmap the brand kit already exports, used when actool cannot render the Icon Composer document.
APP_ICON_FALLBACK = ROOT / "assets" / "brand-kit" / "native-app" / "ios" / "AppIcon.appiconset" / "AppIcon-1024.png"
# What iconutil expects in an .iconset, largest first so sips only ever shrinks the 1024px master.
ICONSET_IMAGES = (
    ("icon_512x512@2x.png", 1024),
    ("icon_512x512.png", 512),
    ("icon_256x256@2x.png", 512),
    ("icon_256x256.png", 256),
    ("icon_128x128@2x.png", 256),
    ("icon_128x128.png", 128),
    ("icon_32x32@2x.png", 64),
    ("icon_32x32.png", 32),
    ("icon_16x16@2x.png", 32),
    ("icon_16x16.png", 16),
)
APP_ID = "org.elarisbrowser.Elaris"
ENGINE_BUILD = ROOT / "build" / "cpp-ladybird"
LADYBIRD_APP = ENGINE_BUILD / "bin" / "Ladybird.app"
# Ladybird's packager rewrites rpaths, copies linked libraries, and re-signs; Elaris reuses it rather than repeat it.
LADYBIRD_MACOS_TOOL = ENGINE / "Meta" / "package-macos-test-bundle.py"
# Elaris's app icon replaces Ladybird's own, so the engine's copies of these two are left behind.
LADYBIRD_APP_ICON_FILES = ("Assets.car", "app_icon.icns")
# Ladybird's own app binary; Elaris ships only the helper processes.
LADYBIRD_APP_BINARY = "Ladybird"


def _copy_if_changed(source: Path, destination: Path) -> bool:
    """Copy `source` over `destination` when the source is newer. Returns whether it copied.

    The copy takes a fresh modification time on purpose: Launch Services caches an app's icon by file time,
    so a copy that kept the source's older time can keep showing a stale icon in the Dock. Times are compared,
    not contents, because signing rewrites the bundled executable and it would otherwise never match again.
    """
    if destination.exists() and destination.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    shutil.copymode(source, destination)
    return True


def _macos_flat_icon(output: Path) -> None:
    """Render Icon.icns from the brand's 1024px export, for hosts whose actool cannot read the Icon Composer document."""
    iconset = output / "Icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)
    for name, size in ICONSET_IMAGES:
        run("sips", "-z", str(size), str(size), str(APP_ICON_FALLBACK), "--out", str(iconset / name))
    run("iconutil", "--convert", "icns", str(iconset), "--output", str(output / "Icon.icns"))
    shutil.rmtree(iconset)


def _macos_app_icon(channel: str) -> Path:
    """Compile the channel's icon into Assets.car (and Icon.icns when actool still emits one), cached under build/."""
    source = APP_ICON_SOURCES[channel]
    output = ROOT / "build" / "icons" / "macos" / source.stem
    newest_source = max(path.stat().st_mtime_ns for path in source.rglob("*") if path.is_file())
    compiled = [path for path in (output / "Assets.car", output / "Icon.icns") if path.is_file()]
    if compiled and min(path.stat().st_mtime_ns for path in compiled) >= newest_source:
        return output

    # actool names the icon after its document, and Info.plist expects "Icon", so compile a copy by that name.
    staged = output / "source" / "Icon.icon"
    if staged.parent.exists():
        shutil.rmtree(staged.parent)
    shutil.copytree(source, staged)
    # actool renders the document through an asset runtime agent that loads the host's frameworks; on macOS 15
    # that agent dies on the symbols macOS 26 added. Lightweight mode uses the runtime bundled with Xcode instead.
    result = subprocess.run(
        (
            "xcrun", "actool", str(staged),
            "--compile", str(output),
            "--output-partial-info-plist", str(output / "partial-info.plist"),
            "--app-icon", "Icon",
            "--platform", "macosx",
            "--target-device", "mac",
            "--minimum-deployment-target", "26.0",
            "--development-region", "en",
            "--enable-on-demand-resources", "NO",
            "--output-format", "human-readable-text",
            "--lightweight-asset-runtime-mode=enabled",
            "--errors", "--warnings",
        ),
        cwd=ROOT,
        check=False,
    )
    assets = output / "Assets.car"
    if result.returncode or not assets.is_file():
        # Keep a release shippable on hosts Xcode 26 no longer supports for Icon Composer documents.
        assets.unlink(missing_ok=True)
        ui.warning(f"actool could not compile {source.name}; falling back to the brand's flat 1024px icon")
        _macos_flat_icon(output)
    return output


def _macos_info_plist(channel: str) -> Path:
    """The Info.plist with the build's channel under `ElarisChannel`, rewritten only when it changes."""
    with (PACKAGING / "macos" / "Info.plist").open("rb") as source:
        info = plistlib.load(source)
    info["ElarisChannel"] = channel
    output = ROOT / "build" / "macos" / f"Info-{channel}.plist"
    rendered = plistlib.dumps(info)
    if not output.is_file() or output.read_bytes() != rendered:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(rendered)
    return output


def _macos_bundle_metadata(contents: Path, channel: str) -> bool:
    """Copy the channel's Info.plist and compiled icon into an app's Contents. Returns whether anything changed."""
    icon = _macos_app_icon(channel)
    # Xcode stopped emitting Icon.icns beside Assets.car, and macOS 26 reads the icon from the catalog.
    sources = [(_macos_info_plist(channel), contents / "Info.plist")]
    for name in ("Assets.car", "Icon.icns"):
        if (icon / name).is_file():
            sources.append((icon / name, contents / "Resources" / name))
    changed = False
    for source, destination in sources:
        changed |= _copy_if_changed(source, destination)
    return changed


def macos_app_bundle(executable: Path, name: str = "ElarisDev") -> Path:
    """A development .app around the executable. Ladybird's helpers stay in the build tree, which the bridge falls
    back to when no packaged helpers sit beside the executable. Returns the bundled executable."""
    contents = ROOT / "build" / f"{name}.app" / "Contents"
    bundled_executable = contents / "MacOS" / executable.name
    copied = _copy_if_changed(executable, bundled_executable)
    copied |= _macos_bundle_metadata(contents, "dev")
    if copied:
        # Replacing any file in a signed bundle invalidates its signature, and macOS kills the app for that.
        subprocess.run(["codesign", "--force", "--sign", "-", str(contents.parent)], check=True)
        # Launch Services also keys its icon cache on the bundle folder's time, so refresh it with the contents.
        contents.parent.touch()
    return bundled_executable


def _ladybird_macos_tool():
    """Load Ladybird's macOS bundle packager from the pinned engine checkout."""
    spec = importlib.util.spec_from_file_location("ladybird_package_macos_test_bundle", LADYBIRD_MACOS_TOOL)
    if spec is None or spec.loader is None:
        raise SystemExit(f"elaris: Ladybird's macOS packager is missing at {LADYBIRD_MACOS_TOOL}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# What the last full bundle was assembled from: the engine's helpers and Elaris's executable.
RELEASE_BUNDLE_STAMP = ROOT / "build" / "Elaris.app.stamp"


def _bundle_inputs(executable: Path) -> tuple[str, str]:
    """When the engine (its helpers and Elaris's bridge library) and Elaris's executable last changed."""
    helpers = [helper for helper in (LADYBIRD_APP / "Contents" / "MacOS").iterdir() if helper.is_file()]
    # The bundle carries its own copy of the bridge, which must export what the executable was built against.
    helpers += list((ENGINE_BUILD / "lib").glob("libelaris_ladybird.*"))
    engine = max(helper.stat().st_mtime_ns for helper in helpers)
    return str(engine), str(executable.stat().st_mtime_ns)


def macos_release_bundle(executable: Path, channel: str = "stable", *, fresh: bool = False) -> Path:
    """build/Elaris.app with Ladybird's helpers, resources and libraries, relocatable and signed.

    A full assembly copies and verifies every engine library, which takes about half a minute, so it runs only
    when the engine changed. When only Elaris changed, its executable is swapped in and the bundle re-signed;
    when nothing changed, the bundle is reused as is. `channel` picks the icon. Returns the bundled executable.
    """
    if not (LADYBIRD_APP / "Contents" / "MacOS" / LADYBIRD_APP_BINARY).is_file():
        raise SystemExit("elaris: Ladybird's app bundle is missing; run ./elaris build first")
    tool = _ladybird_macos_tool()

    app = ROOT / "build" / "Elaris.app"
    contents = app / "Contents"
    bundled_executable = contents / "MacOS" / executable.name
    engine, elaris = _bundle_inputs(executable)
    previous = RELEASE_BUNDLE_STAMP.read_text().split() if RELEASE_BUNDLE_STAMP.is_file() else []
    if not fresh and bundled_executable.is_file() and len(previous) == 2 and previous[0] == engine:
        icon_changed = _macos_bundle_metadata(contents, channel)
        if previous[1] != elaris or icon_changed:
            ui.info("Updating Elaris in Elaris.app…")
            shutil.copy2(executable, bundled_executable)
            tool.fix_rpaths(app)
            tool.resign(app, ENGINE_BUILD)
            app.touch()
            RELEASE_BUNDLE_STAMP.write_text(f"{engine} {elaris}")
        return bundled_executable

    ui.info("Bundling Elaris.app with Ladybird's engine (about 30 seconds)…")
    RELEASE_BUNDLE_STAMP.unlink(missing_ok=True)
    if app.exists():
        shutil.rmtree(app)
    macos = contents / "MacOS"
    macos.mkdir(parents=True)
    shutil.copy2(executable, bundled_executable)
    for helper in (LADYBIRD_APP / "Contents" / "MacOS").iterdir():
        if helper.is_file() and helper.name != LADYBIRD_APP_BINARY:
            shutil.copy2(helper, macos / helper.name)
    shutil.copytree(
        LADYBIRD_APP / "Contents" / "Resources",
        contents / "Resources",
        ignore=shutil.ignore_patterns(*LADYBIRD_APP_ICON_FILES),
    )
    _macos_bundle_metadata(contents, channel)

    # Libraries come from the directories the built executables were linked against, which is what dyld searched.
    search_directories = []
    for binary in (executable, macos / "WebContent"):
        for entry in tool.rpath_entries(binary):
            directory = Path(entry)
            if entry.startswith("/") and directory.is_dir() and directory not in search_directories:
                search_directories.append(directory)
    tool.fix_rpaths(app)
    copied = tool.copy_linked_libraries(app, search_directories)
    tool.fix_rpaths(app)
    tool.resign(app, ENGINE_BUILD)
    tool.verify(app)
    ui.info(f"copied {len(copied)} libraries into {app.name}/Contents/lib")
    RELEASE_BUNDLE_STAMP.write_text(f"{engine} {elaris}")
    return bundled_executable


def _elf_needed(binary: Path) -> list[str]:
    result = subprocess.run(["readelf", "-d", str(binary)], capture_output=True, text=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise SystemExit(f"elaris: could not read the libraries {binary} needs: {detail}")
    output = result.stdout
    return re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", output)


def _linux_search_directories() -> list[Path]:
    """The directories the engine's libraries were built into: Lagom's and vcpkg's."""
    directories = [ENGINE_BUILD / "lib", VCPKG_INSTALLED / vcpkg_triplet() / "lib"]
    return [directory for directory in directories if directory.is_dir()]


def _copy_linux_libraries(binaries: list[Path], library_directory: Path) -> list[str]:
    """Copy each library the binaries need that a build directory has, transitively. Libraries the system provides
    (the C library, libstdc++, the graphics stack) are left to the system."""
    search_directories = _linux_search_directories()
    library_directory.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    pending = list(binaries)
    seen = {binary.resolve() for binary in pending}
    while pending:
        binary = pending.pop()
        for name in _elf_needed(binary):
            source = next((directory / name for directory in search_directories if (directory / name).is_file()), None)
            if source is None:
                continue
            destination = library_directory / name
            if not destination.exists():
                shutil.copyfile(source.resolve(), destination)
                shutil.copymode(source.resolve(), destination)
                copied.append(name)
            if destination.resolve() not in seen:
                seen.add(destination.resolve())
                pending.append(destination)
    return copied


def linux_prefix_bundle(executable: Path, prefix: Path, *, fresh: bool = False) -> Path:
    """A prefix tree (bin/, lib/, libexec/, share/) that copying into ~/.local or /usr installs whole.

    Ladybird's layout is what its helpers search: they live in libexec/, their resources in share/Lagom, and each
    binary finds the libraries in ../lib through its RUNPATH. Returns the installed executable.
    """
    if not (ENGINE_BUILD / "libexec" / "WebContent").is_file():
        raise SystemExit("elaris: Ladybird's helper processes are missing; run ./elaris build first")
    if shutil.which("patchelf") is None:
        raise SystemExit("elaris: patchelf is required to bundle Linux libraries; install it and rerun ./elaris bundle")

    installed = prefix / "bin" / executable.name
    helpers = sorted(path for path in (ENGINE_BUILD / "libexec").iterdir() if path.is_file())
    library_directory = prefix / "lib"
    if not fresh and _linux_engine_key() == _read_stamp(LINUX_BUNDLE_STAMP) and installed.parent.is_dir():
        # The engine did not change, so the prefix stays and only Elaris's executable and any libraries it newly needs
        # are refreshed. Its helpers and resources are the same files as before.
        shutil.copy2(executable, installed)
        _copy_linux_libraries([installed], library_directory)
    else:
        # Assembled from scratch, so no stale library or helper survives an engine rebuild. The stamp goes first, so an
        # interrupted assembly cannot pass for a complete one.
        LINUX_BUNDLE_STAMP.unlink(missing_ok=True)
        if prefix.exists():
            shutil.rmtree(prefix)
        installed.parent.mkdir(parents=True)
        shutil.copy2(executable, installed)
        (prefix / "libexec").mkdir()
        for helper in helpers:
            shutil.copy2(helper, prefix / "libexec" / helper.name)
        _copy_linux_libraries([installed, *(prefix / "libexec" / helper.name for helper in helpers)], library_directory)

        shutil.copytree(ENGINE_BUILD / "share" / "Lagom", prefix / "share" / "Lagom")
        applications = prefix / "share" / "applications"
        applications.mkdir(parents=True)
        shutil.copy2(LINUX_ASSETS / f"{APP_ID}.desktop", applications / f"{APP_ID}.desktop")
        icon_root = LINUX_ASSETS / "icons"
        for source in icon_root.rglob("*"):
            if source.is_file():
                destination = prefix / "share" / "icons" / source.relative_to(icon_root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        LINUX_BUNDLE_STAMP.parent.mkdir(parents=True, exist_ok=True)
        LINUX_BUNDLE_STAMP.write_text(_linux_engine_key())

    run_path = {installed: "$ORIGIN/../lib"}
    run_path.update({prefix / "libexec" / helper.name: "$ORIGIN/../lib" for helper in helpers})
    run_path.update({library: "$ORIGIN" for library in library_directory.iterdir() if library.is_file()})
    for binary, origin in run_path.items():
        # ICU's data library holds its whole payload in one symbol that points into .rodata. A patchelf
        # rewrite relocates that section and leaves the symbol naming filler bytes, so ICU then finds no
        # data at runtime (U_MISSING_RESOURCE_ERROR) and WebContent traps on any page with non-ASCII text.
        # The library has no dependencies of its own, so it does not need the RUNPATH we would set here.
        if binary.name.startswith("libicudata."):
            continue
        subprocess.run(["patchelf", "--set-rpath", origin, str(binary)], check=True)
    _verify_linux_prefix(prefix)
    return installed


# What the last full Linux prefix was assembled from. A matching key lets the next bundle skip the engine copy.
LINUX_BUNDLE_STAMP = ROOT / "build" / "linux" / "Elaris.stamp"


def _read_stamp(path: Path) -> str:
    return path.read_text() if path.is_file() else ""


def _linux_engine_key() -> str:
    """The engine's inputs to the Linux prefix: its dependency fingerprint and when its helpers and bridge last changed."""
    helpers = [path for path in (ENGINE_BUILD / "libexec").iterdir() if path.is_file()]
    bridge = list((ENGINE_BUILD / "lib").glob("libelaris_ladybird.*"))
    newest = max((path.stat().st_mtime_ns for path in helpers + bridge), default=0)
    return f"{engine.dependencies_fingerprint()} {newest}"


def _verify_linux_prefix(prefix: Path) -> None:
    """Fail if a binary's RUNPATH still names the build tree, since the bundle must run from anywhere."""
    for binary in sorted(path for path in prefix.rglob("*") if path.is_file() and not path.is_symlink()):
        if subprocess.run(["readelf", "-h", str(binary)], capture_output=True).returncode != 0:
            continue
        output = subprocess.run(["readelf", "-d", str(binary)], capture_output=True, text=True).stdout
        for runpath in re.findall(r"\((?:RUNPATH|RPATH)\)\s+Library (?:runpath|rpath): \[([^\]]*)\]", output):
            if any(entry.startswith("/") for entry in runpath.split(":")):
                raise SystemExit(f"elaris: {binary.relative_to(prefix)} still has an absolute RUNPATH: {runpath}")
