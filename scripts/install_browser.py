"""Install the browser the application uses, into the application's own folder.

The app does this by itself on first run; this script exists so a developer
or a build machine can do it ahead of time, and so the integration tests have
something to run against.

It deliberately installs into ``%LOCALAPPDATA%\\Cartwright\\browser\\
playwright`` rather than Playwright's default location or the package
directory. Installing into the package directory
(``PLAYWRIGHT_BROWSERS_PATH=0``) would cause PyInstaller to bundle ~430 MB of
browser into the build, which ``scripts/build.py`` refuses to do.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.automation.browser_manager import BrowserManager  # noqa: E402
from app.paths import get_paths  # noqa: E402


def main() -> int:
    paths = get_paths().ensure()
    manager = BrowserManager(paths)

    existing = manager.chromium_executable()
    if existing is not None:
        print(f"Browser already installed:\n  {existing}")  # noqa: T201
        info = manager.info()
        print(f"  {info.version}, Playwright {info.playwright_version}")  # noqa: T201
        return 0

    print(f"Installing the browser into:\n  {paths.playwright_browsers_dir}")  # noqa: T201
    print("This is about a 200 MB download and happens once.")  # noqa: T201

    last = ""

    def report(message: str) -> None:
        nonlocal last
        if message != last:
            last = message
            print(f"  {message}")  # noqa: T201

    manager.install_chromium(on_progress=report)
    executable = manager.chromium_executable()
    print(f"Done:\n  {executable}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
