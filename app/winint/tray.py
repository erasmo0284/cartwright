"""The system tray icon and its menu.

The application keeps working with its window closed, so the tray icon is the
only thing on screen most of the time. It has to answer "is it still
watching?" and "does anything need me?" at a glance, and it has to offer the
few actions worth taking without opening the window.

Two Qt and Windows details are handled here rather than in the UI layer:

* ``setContextMenu`` does not take ownership of the menu, so the menu is held
  on this object. A menu built in a local variable is destroyed when the
  function returns, leaving the tray icon pointing at freed memory.
* Windows copies the tooltip into a fixed 128-character buffer
  (``NOTIFYICONDATA.szTip``), so the tooltip is truncated to 127 characters
  rather than trusting the platform to cope.

Icons are supplied by a callable from the UI layer instead of being loaded
here, so this module has no opinion about themes, file names or resource
paths, and the tests do not need image files.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from app.branding import BRAND
from app.diagnostics.logger import get_logger

logger = get_logger("winint.tray")

#: Windows' ``szTip`` buffer is 128 wide characters including the terminator.
MAX_TOOLTIP_CHARS = 127

#: How often the icon re-asserts itself. If ``explorer.exe`` restarts, every
#: tray icon disappears and each application is expected to add its own back.
_REASSERT_INTERVAL_MS = 60_000

#: The state names passed to the icon provider.
STATE_OFF = "off"
STATE_PAUSED = "paused"
STATE_ATTENTION = "attention"
STATE_WATCHING = "watching"
STATE_IDLE = "idle"


class TrayController(QObject):
    """Owns the tray icon, its menu and the text it shows.

    ``icon_provider`` is called with one of the ``STATE_*`` names and returns
    the icon to display.
    """

    open_requested = Signal()
    pause_toggled = Signal(bool)
    check_now_requested = Signal()
    exit_requested = Signal()

    def __init__(
        self,
        icon_provider: Callable[[str], QIcon],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._icon_provider = icon_provider
        self._state = STATE_IDLE
        self._should_be_visible = False

        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(icon_provider(self._state))
        self._tray.activated.connect(self._on_activated)

        # Held on self: Qt does not take ownership of a menu given to
        # setContextMenu, and a local one would be destroyed immediately.
        self._menu = QMenu()
        self._open_action = self._menu.addAction(f"Open {BRAND.short_name}")
        self._open_action.triggered.connect(self.open_requested.emit)
        self._menu.addSeparator()
        self._pause_action = self._menu.addAction("Pause monitoring")
        self._pause_action.setCheckable(True)
        self._pause_action.toggled.connect(self.pause_toggled.emit)
        self._check_action = self._menu.addAction("Check now")
        self._check_action.triggered.connect(self.check_now_requested.emit)
        self._menu.addSeparator()
        self._exit_action = self._menu.addAction("Exit")
        self._exit_action.triggered.connect(self.exit_requested.emit)
        self._tray.setContextMenu(self._menu)

        self.set_state(watching=0, needs_attention=0, paused=False, monitoring_enabled=True)

        self._reassert_timer = QTimer(self)
        self._reassert_timer.setInterval(_REASSERT_INTERVAL_MS)
        self._reassert_timer.timeout.connect(self._reassert)

    @staticmethod
    def is_available() -> bool:
        """True when this desktop has a tray to put an icon in.

        Some Windows configurations and remote sessions have none, and the
        application has to stay usable without it rather than assuming the
        icon is there.
        """
        return bool(QSystemTrayIcon.isSystemTrayAvailable())

    def set_state(
        self,
        watching: int,
        needs_attention: int,
        paused: bool,
        monitoring_enabled: bool,
    ) -> None:
        """Update the icon, the tooltip and the menu to match the app state."""
        if not monitoring_enabled:
            state = STATE_OFF
        elif paused:
            state = STATE_PAUSED
        elif needs_attention > 0:
            state = STATE_ATTENTION
        elif watching > 0:
            state = STATE_WATCHING
        else:
            state = STATE_IDLE

        if state != self._state:
            self._state = state
            self._tray.setIcon(self._icon_provider(state))

        self._tray.setToolTip(
            build_tooltip(
                watching=watching,
                needs_attention=needs_attention,
                paused=paused,
                monitoring_enabled=monitoring_enabled,
            )
        )

        self._pause_action.setEnabled(monitoring_enabled)
        self._check_action.setEnabled(monitoring_enabled and not paused)
        # Reflect the real state without reporting it back as a user action.
        self._pause_action.blockSignals(True)
        self._pause_action.setChecked(paused)
        self._pause_action.blockSignals(False)

    def tooltip(self) -> str:
        """The tooltip currently set, for tests and the diagnostics report."""
        return self._tray.toolTip()

    def show_message(self, title: str, body: str, seconds: int = 10) -> None:
        """Show a tray balloon.

        This is the fallback used when a real Action Center toast cannot be
        sent, so it deliberately takes the same plain arguments.
        """
        self._tray.showMessage(
            title, body, QSystemTrayIcon.MessageIcon.Information, max(1, seconds) * 1000
        )

    def show(self) -> None:
        """Put the icon in the tray and keep it there."""
        self._should_be_visible = True
        self._tray.show()
        self._reassert_timer.start()

    def hide(self) -> None:
        """Take the icon out of the tray."""
        self._should_be_visible = False
        self._reassert_timer.stop()
        self._tray.hide()

    def _reassert(self) -> None:
        if self._should_be_visible and not self._tray.isVisible():
            logger.info("Putting the tray icon back after the taskbar restarted")
            self._tray.show()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.open_requested.emit()


def build_tooltip(
    *,
    watching: int,
    needs_attention: int,
    paused: bool,
    monitoring_enabled: bool,
) -> str:
    """The tray tooltip text, capped at the length Windows can store."""
    lines = [BRAND.display_name]
    if not monitoring_enabled:
        lines.append("Monitoring is off")
    elif paused:
        lines.append("Monitoring is paused")
    lines.append(f"Watching: {watching}")
    lines.append(f"Needs attention: {needs_attention}")
    return "\n".join(lines)[:MAX_TOOLTIP_CHARS]
