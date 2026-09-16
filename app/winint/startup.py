r"""Start with Windows, without administrator rights.

The entry is a single value under::

    HKCU\Software\Microsoft\Windows\CurrentVersion\Run

which is per-user, needs no elevation and no scheduled task, and is the
location Windows itself shows in Settings -> Apps -> Startup.

Reading only that value is not enough to answer "will this start at logon?".
When the user turns the entry off in Settings, Windows leaves the ``Run``
value exactly where it is and instead writes a 12-byte binary value under::

    HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run

The first byte of that blob carries the switch: bit 0 clear means enabled
(``0x02``/``0x06``), bit 0 set means the user disabled it (``0x03``/``0x07``).
The remaining bytes are a timestamp Windows uses for its own bookkeeping. A
naive "is my Run value present?" check therefore reports autostart as on for a
user who has explicitly turned it off, and the settings screen would
contradict Windows.

``StartupApproved`` is the user's switch and this module never writes to it.
If the user turned the application off there, the honest answer is to say so
and offer to open the Windows screen where they can turn it back on;
re-enabling it behind their back would be overriding a decision they made.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from app.branding import BRAND
from app.diagnostics.logger import get_logger
from app.paths import executable_path, is_frozen

logger = get_logger("winint.startup")

#: Registry subkeys, relative to ``HKEY_CURRENT_USER``. Module level so a test
#: can redirect them to a scratch key instead of the real ones.
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"

#: Deep link to Settings -> Apps -> Startup.
_SETTINGS_URI = "ms-settings:startupapps"


@dataclass(frozen=True)
class StartupStatus:
    """What will actually happen at the next logon."""

    #: The application's own ``Run`` value exists.
    registered: bool
    #: The user switched the entry off in Windows Settings.
    disabled_by_windows: bool
    #: The registered command line, or ``None`` when not registered.
    command: str | None
    #: Registered and not switched off: the application really will start.
    effective: bool

    @property
    def summary(self) -> str:
        """One short line for the settings screen."""
        if self.disabled_by_windows:
            return "Turned off in Windows Settings"
        return "On" if self.effective else "Off"


def startup_command(extra_args: str = "--tray") -> str:
    """The command line to register, with every path quoted.

    From a frozen build this is the executable itself. From source it prefers
    ``pythonw.exe`` next to the running interpreter, because ``python.exe``
    would flash a console window on the user's screen at every logon.
    """
    if is_frozen():
        return _join(str(executable_path()), extra_args)
    return _join(str(_source_launcher()), "-m", "app.main", extra_args)


def enable(extra_args: str = "--tray") -> None:
    """Register the application to start at logon. Idempotent."""
    if not _available():
        return
    import winreg

    command = startup_command(extra_args)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, BRAND.startup_registry_value, 0, winreg.REG_SZ, command)
    logger.info("Start with Windows turned on", extra={"command": command})


def disable() -> None:
    """Remove the logon entry. Does nothing when it is not there."""
    if not _available():
        return
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, BRAND.startup_registry_value)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Could not turn off start with Windows: %s", exc)
        return
    logger.info("Start with Windows turned off")


def is_enabled() -> bool:
    """True when the application really will start at the next logon."""
    return status().effective


def status() -> StartupStatus:
    """The full picture, including the switch in Windows Settings."""
    command = _read_run_value()
    registered = command is not None
    disabled = _disabled_in_windows_settings()
    return StartupStatus(
        registered=registered,
        disabled_by_windows=disabled,
        command=command,
        effective=registered and not disabled,
    )


def open_windows_startup_settings() -> None:
    """Open Settings -> Apps -> Startup so the user can flip their own switch."""
    QDesktopServices.openUrl(QUrl(_SETTINGS_URI))


def _read_run_value() -> str | None:
    if not _available():
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, BRAND.startup_registry_value)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Could not read the start with Windows setting: %s", exc)
        return None
    return str(value)


def _disabled_in_windows_settings() -> bool:
    """True when the user switched the entry off in Settings -> Startup."""
    if not _available():
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _APPROVED_KEY, 0, winreg.KEY_READ
        ) as key:
            blob, _ = winreg.QueryValueEx(key, BRAND.startup_registry_value)
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("Could not read the Windows startup approval: %s", exc)
        return False
    return is_disabled_blob(blob)


def is_disabled_blob(blob: object) -> bool:
    """Interpret a ``StartupApproved`` value: True when it means "off".

    Separate from the registry read so the byte rule can be tested directly,
    and so an unexpected value shape is treated as "not disabled" rather than
    hiding a working autostart entry.
    """
    if not isinstance(blob, (bytes, bytearray)) or not blob:
        return False
    return bool(blob[0] & 0x01)


def _source_launcher() -> Path:
    """``pythonw.exe`` beside the running interpreter, else the interpreter."""
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name("pythonw.exe")
    if windowless.exists():
        return windowless
    return interpreter


def _join(program: str, *arguments: str) -> str:
    parts = [f'"{program}"']
    parts.extend(_quote(argument) for argument in arguments if argument)
    return " ".join(parts)


def _quote(argument: str) -> str:
    return f'"{argument}"' if " " in argument else argument


def _available() -> bool:
    """False off Windows, where there is no registry to read or write."""
    return sys.platform == "win32"
