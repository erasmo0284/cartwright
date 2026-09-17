r"""Filesystem layout for application data and bundled resources.

All mutable application state lives under a single per-user directory so that
uninstalling the program never needs to touch anything else, and so a support
request can be satisfied by looking in one place::

    %LOCALAPPDATA%\AmazonPurchaseBot\
        data\app.db
        browser\amazon-profile\
        browser\playwright\
        logs\
        screenshots\
        backups\
        config\

The root can be redirected with the ``APB_DATA_DIR`` environment variable,
which is what the test suite and the portable build use.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.branding import BRAND

DATA_DIR_ENV_VAR = "APB_DATA_DIR"


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle rather than source."""
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Directory that bundled read-only resources are extracted/installed to.

    Under PyInstaller ``sys._MEIPASS`` points at the onefile extraction dir or
    the onedir ``_internal`` folder. From source it is the repository root.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    """Absolute path to a bundled read-only resource (icons, QSS, fixtures)."""
    return bundle_root().joinpath(*parts)


def executable_path() -> Path:
    """Path used to re-launch the application (for "start with Windows").

    When frozen this is the real ``.exe``. From source it is the interpreter,
    and the caller is expected to append the ``-m app.main`` arguments.
    """
    return Path(sys.executable).resolve()


def _default_root() -> Path:
    override = os.environ.get(DATA_DIR_ENV_VAR)
    if override:
        return Path(override).expanduser().resolve()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data)
    else:
        # Non-Windows (test/CI) fallback so the module stays importable.
        base = Path.home() / ".local" / "share"
    return (base / BRAND.data_folder_name).resolve()


@dataclass(frozen=True)
class AppPaths:
    """Resolved application directories.

    Instances are immutable; call :meth:`ensure` once at startup to create the
    directories on disk.
    """

    root: Path

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def database_file(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def browser_dir(self) -> Path:
        return self.root / "browser"

    @property
    def browser_profile_dir(self) -> Path:
        """Dedicated Chrome/Chromium profile. Never the user's own profile."""
        return self.browser_dir / "amazon-profile"

    @property
    def playwright_browsers_dir(self) -> Path:
        """Where Playwright keeps its downloaded browser builds."""
        return self.browser_dir / "playwright"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def screenshots_dir(self) -> Path:
        """Automation diagnostics captures. User-clearable."""
        return self.root / "screenshots"

    @property
    def images_dir(self) -> Path:
        """Product thumbnails, photographed from the page the browser showed.

        A cache and nothing more: deleting it costs a placeholder until the
        next check. Kept apart from ``screenshots_dir``, which holds
        diagnostics of failures and is offered to the user for clearing.
        """
        return self.root / "images"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def lock_file(self) -> Path:
        return self.root / "app.lock"

    def all_directories(self) -> tuple[Path, ...]:
        return (
            self.root,
            self.data_dir,
            self.browser_dir,
            self.browser_profile_dir,
            self.playwright_browsers_dir,
            self.logs_dir,
            self.screenshots_dir,
            self.images_dir,
            self.backups_dir,
            self.config_dir,
        )

    def ensure(self) -> AppPaths:
        """Create every application directory. Idempotent."""
        for directory in self.all_directories():
            directory.mkdir(parents=True, exist_ok=True)
        return self


@lru_cache(maxsize=1)
def get_paths() -> AppPaths:
    """The process-wide :class:`AppPaths`. Cached after first resolution."""
    return AppPaths(root=_default_root())


def reset_paths_cache() -> None:
    """Forget the cached root. Used by tests that relocate ``APB_DATA_DIR``."""
    get_paths.cache_clear()
