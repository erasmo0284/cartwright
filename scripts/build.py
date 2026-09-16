"""Build the Windows application and, optionally, the installer.

Usage::

    .venv\\Scripts\\python.exe scripts\\build.py            # build the app
    .venv\\Scripts\\python.exe scripts\\build.py --installer # app + setup.exe
    .venv\\Scripts\\python.exe scripts\\build.py --clean

The script does the things that are easy to forget and expensive to get
wrong:

* generates the application icon and the Windows version resource, so the
  executable has proper file metadata (which materially affects how
  SmartScreen and antivirus treat an unsigned binary);
* refuses to build if a Chromium has been installed *inside* the package
  directory, because Playwright's own PyInstaller hook would then sweep
  ~430 MB of browser into the bundle without saying anything;
* reports the resulting size, so a regression in what is being bundled is
  visible rather than discovered by a user on a slow connection.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.branding import BRAND  # noqa: E402
from app.version import BUILD_CHANNEL, VERSION  # noqa: E402

BUILD_DIR = PROJECT_ROOT / "build"
DIST_DIR = PROJECT_ROOT / "dist"
SPEC_FILE = PROJECT_ROOT / "installer" / "app.spec"
ISS_FILE = PROJECT_ROOT / "installer" / "setup.iss"
OUTPUT_DIR = PROJECT_ROOT / "installer" / "output"

#: Where Inno Setup's compiler may live. The per-user location comes first
#: because that is where ``winget install --scope user`` puts it, and a
#: per-user install is what a developer without administrator rights gets.
ISCC_CANDIDATES = (
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe"),
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 7\ISCC.exe",
    r"C:\Program Files\Inno Setup 7\ISCC.exe",
    r"C:\tools\InnoSetup\ISCC.exe",
)


def log(message: str) -> None:
    print(f"[build] {message}", flush=True)  # noqa: T201


def fail(message: str) -> None:
    print(f"[build] ERROR: {message}", file=sys.stderr, flush=True)  # noqa: T201
    raise SystemExit(1)


def clean() -> None:
    """Remove previous build output."""
    for directory in (BUILD_DIR, DIST_DIR, OUTPUT_DIR):
        if directory.exists():
            log(f"removing {directory.relative_to(PROJECT_ROOT)}")
            shutil.rmtree(directory, ignore_errors=True)


def check_no_bundled_browser() -> None:
    """Refuse to build with a browser inside the Playwright package.

    ``PLAYWRIGHT_BROWSERS_PATH=0`` installs Chromium into
    ``site-packages/playwright/driver/package/.local-browsers``, and
    Playwright's PyInstaller hook collects that directory wholesale. The
    result is a working but enormous build, so this is a hard error rather
    than a warning.
    """
    try:
        import playwright
    except ImportError:  # pragma: no cover - dependency is required to build
        fail("playwright is not installed; run: pip install -r requirements-dev.txt")
        return

    local_browsers = (
        Path(playwright.__file__).parent / "driver" / "package" / ".local-browsers"
    )
    if local_browsers.exists() and any(local_browsers.iterdir()):
        fail(
            "A browser is installed inside the playwright package at\n"
            f"  {local_browsers}\n"
            "It would be bundled into the build (~430 MB). Delete that "
            "directory, then re-install the browser with\n"
            "  python scripts/install_browser.py"
        )


def write_version_resource() -> Path:
    """Write the Windows VERSIONINFO resource PyInstaller embeds."""
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    destination = BUILD_DIR / "version_info.txt"

    parts = [int(piece) for piece in VERSION.split(".")]
    while len(parts) < 4:
        parts.append(0)
    numbers = ", ".join(str(piece) for piece in parts[:4])

    destination.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({numbers}),
    prodvers=({numbers}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', {BRAND.publisher!r}),
         StringStruct('FileDescription', {BRAND.display_name!r}),
         StringStruct('FileVersion', {VERSION!r}),
         StringStruct('InternalName', {BRAND.exe_name!r}),
         StringStruct('OriginalFilename', {(BRAND.exe_name + '.exe')!r}),
         StringStruct('ProductName', {BRAND.display_name!r}),
         StringStruct('ProductVersion', {VERSION!r}),
         StringStruct('Comments', {BRAND.disclaimer!r})])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
        encoding="utf-8",
    )
    log(f"version resource written ({VERSION}, {BUILD_CHANNEL})")
    return destination


def write_icon() -> Path:
    """Render the application icon to a multi-resolution .ico.

    Needed by the executable, the installer and the notification
    registration, all of which want a real file rather than a QIcon.
    """
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    destination = BUILD_DIR / "app.ico"

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from app.ui.theme.icons import write_ico

    owns_app = QApplication.instance() is None
    application = QApplication([]) if owns_app else QApplication.instance()
    try:
        write_ico(destination)
    finally:
        if owns_app and application is not None:
            application.quit()

    if not destination.exists() or destination.stat().st_size == 0:
        fail("the application icon could not be generated")
    log(f"icon written ({destination.stat().st_size / 1024:.0f} KiB)")
    return destination


def run_pyinstaller() -> Path:
    """Build the application. Returns the onedir output folder."""
    if not SPEC_FILE.exists():
        fail(f"missing spec file: {SPEC_FILE}")

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR / "pyinstaller"),
        str(SPEC_FILE),
    ]
    log("running PyInstaller (this takes a few minutes)")
    result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    if result.returncode != 0:
        fail(f"PyInstaller failed with exit code {result.returncode}")

    output = DIST_DIR / BRAND.exe_name
    executable = output / f"{BRAND.exe_name}.exe"
    if not executable.exists():
        fail(f"expected executable not found: {executable}")
    return output


def report_size(folder: Path) -> None:
    total = sum(path.stat().st_size for path in folder.rglob("*") if path.is_file())
    log(f"build size: {total / 1024 / 1024:.0f} MB in {folder}")
    largest = sorted(
        (path for path in folder.rglob("*") if path.is_file()),
        key=lambda path: path.stat().st_size,
        reverse=True,
    )[:8]
    for path in largest:
        log(f"  {path.stat().st_size / 1024 / 1024:6.1f} MB  {path.relative_to(folder)}")


def smoke_test(folder: Path) -> None:
    """Run the built executable's ``--version`` to prove it starts."""
    executable = folder / f"{BRAND.exe_name}-debug.exe"
    if not executable.exists():
        log("no debug executable to smoke test; skipping")
        return
    log("smoke testing the built executable")
    result = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 or VERSION not in output:
        fail(
            "the built executable did not report its version:\n"
            f"exit={result.returncode}\n{output[-2000:]}"
        )
    log(f"smoke test passed: {output.strip()}")


def find_iscc() -> str | None:
    for candidate in ISCC_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("ISCC.exe") or shutil.which("iscc")
    return found


def build_installer() -> Path | None:
    """Compile the Inno Setup installer, if the compiler is available."""
    if not ISS_FILE.exists():
        fail(f"missing installer script: {ISS_FILE}")
    iscc = find_iscc()
    if iscc is None:
        log(
            "Inno Setup was not found, so no installer was built.\n"
            "        Install it with:  winget install -e --id JRSoftware.InnoSetup\n"
            "        then re-run this script with --installer."
        )
        return None

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log(f"compiling the installer with {iscc}")
    result = subprocess.run(
        [
            iscc,
            f"/DMyAppVersion={VERSION}",
            f"/O{OUTPUT_DIR}",
            str(ISS_FILE),
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if result.returncode != 0:
        fail(f"Inno Setup failed with exit code {result.returncode}")

    installers = sorted(OUTPUT_DIR.glob("*.exe"), key=lambda p: p.stat().st_mtime)
    if not installers:
        fail("Inno Setup reported success but produced no installer")
    installer = installers[-1]
    log(
        f"installer: {installer} "
        f"({installer.stat().st_size / 1024 / 1024:.0f} MB)"
    )
    return installer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Build {BRAND.display_name}")
    parser.add_argument("--clean", action="store_true", help="remove build output first")
    parser.add_argument(
        "--installer", action="store_true", help="also compile the Windows installer"
    )
    parser.add_argument(
        "--skip-smoke-test", action="store_true", help="do not run the built exe"
    )
    arguments = parser.parse_args(argv)

    if sys.platform != "win32":
        fail("this build produces a Windows application and must run on Windows")

    log(f"{BRAND.display_name} {VERSION} ({BUILD_CHANNEL})")
    log(f"python {sys.version.split()[0]}")

    if arguments.clean:
        clean()

    check_no_bundled_browser()
    write_version_resource()
    write_icon()
    output = run_pyinstaller()
    report_size(output)
    if not arguments.skip_smoke_test:
        smoke_test(output)
    if arguments.installer:
        build_installer()

    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
