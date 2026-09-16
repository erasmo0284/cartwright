r"""Application User Model ID, the identity Windows attaches notifications to.

Windows will only show a toast in the Action Center on behalf of an
application it can identify. Identity comes from an Application User Model ID
(AUMID): the process declares one, and a registry key under::

    HKCU\SOFTWARE\Classes\AppUserModelId\<aumid>

tells Windows the name and icon to draw on the notification.

The failure mode is what makes this module necessary: with no registered
AUMID, Windows **silently drops the toast**. There is no exception, no error
code and nothing in the Action Center -- the notification simply never
appears. Nothing in the application can detect that after the fact, which is
why the notifier registers the AUMID up front, checks the registration, and
falls back to a tray balloon when it cannot be established.

:func:`set_process_aumid` must run before the first window is created.
Windows reads the process identity when the first top-level window appears,
and setting it afterwards leaves notifications and the taskbar grouping
attached to the wrong identity for the life of the process.
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.branding import BRAND
from app.diagnostics.logger import get_logger

logger = get_logger("winint.aumid")

#: Registry subkey, relative to ``HKEY_CURRENT_USER``, holding one child key
#: per AUMID. Module level so a test can redirect it to a scratch key.
_AUMID_KEY_ROOT = r"SOFTWARE\Classes\AppUserModelId"

#: Windows expects a colour string here; "0" means "use the default".
_ICON_BACKGROUND = "0"


def set_process_aumid() -> bool:
    """Declare this process's identity to Windows. True when it was accepted.

    Call before any window exists. Returns False off Windows and whenever the
    call fails, so the notifier can choose the tray balloon instead of
    sending toasts that would vanish.
    """
    if sys.platform != "win32":
        return False

    import ctypes

    try:
        result = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            BRAND.aumid
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not set the notification identity: %s", exc)
        return False

    if result != 0:
        logger.warning("Windows refused the notification identity (code %s)", result)
        return False
    return True


def register_aumid(icon_path: Path | None = None) -> bool:
    """Write the name and icon Windows shows on notifications. Idempotent.

    ``icon_path`` should be an ``.ico`` file; it is recorded only when it
    exists, because a path Windows cannot read makes it draw a blank tile.
    """
    if sys.platform != "win32":
        return False

    import winreg

    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _key_path(), 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, BRAND.display_name)
            winreg.SetValueEx(
                key, "IconBackgroundColor", 0, winreg.REG_SZ, _ICON_BACKGROUND
            )
            if icon_path is not None and icon_path.exists():
                winreg.SetValueEx(
                    key, "IconUri", 0, winreg.REG_SZ, str(icon_path.resolve())
                )
    except OSError as exc:
        logger.warning("Could not register the notification identity: %s", exc)
        return False

    logger.info("Notification identity registered", extra={"aumid": BRAND.aumid})
    return True


def unregister_aumid() -> None:
    """Remove the registration. Does nothing when it is not there."""
    if sys.platform != "win32":
        return

    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _key_path())
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Could not remove the notification identity: %s", exc)


def is_registered() -> bool:
    """True when Windows has a name on file for this application's AUMID."""
    if sys.platform != "win32":
        return False

    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _key_path(), 0, winreg.KEY_READ
        ) as key:
            name, _ = winreg.QueryValueEx(key, "DisplayName")
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return bool(name)


def display_name() -> str | None:
    """The registered name, for the diagnostics report. ``None`` when absent."""
    if sys.platform != "win32":
        return None

    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _key_path(), 0, winreg.KEY_READ
        ) as key:
            name, _ = winreg.QueryValueEx(key, "DisplayName")
    except OSError:
        return None
    return str(name)


def _key_path() -> str:
    return rf"{_AUMID_KEY_ROOT}\{BRAND.aumid}"
