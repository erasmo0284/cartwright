"""Theme selection and application.

One object owns the whole visual state: which appearance is in force, which
token set that implies, and the stylesheet built from it. Widgets never ask
"is it dark?" to pick a colour -- they use an ``objectName`` or a ``role``
property and let the stylesheet answer.

How light/dark actually switches
--------------------------------

Qt 6.8 added ``QStyleHints.setColorScheme``, which is the *only* correct lever
here: it tells Qt's native styles which scheme to draw, so the ``windows11``
style repaints its own controls to match, and it also drives
``QPalette``. Building a palette by hand instead would fight the native style
rather than co-operate with it. Setting the scheme to
:attr:`Qt.ColorScheme.Unknown` hands control back to the operating system,
which is what :attr:`ThemeMode.SYSTEM` means.

The awkward part is the notification: when ``colorSchemeChanged`` fires, the
old palette is still installed -- Qt emits the signal before it has finished
propagating the new scheme. Reading colours or re-applying a stylesheet
synchronously in that slot gives you the previous appearance. Deferring with
``QTimer.singleShot(0, ...)`` runs the re-apply on the next pass of the event
loop, by which time the new scheme is in place.

On Mica and acrylic
-------------------

Deliberately not implemented. The system backdrops require the window to have
a translucent background (``WA_TranslucentBackground`` plus
``DWMWA_SYSTEMBACKDROP_TYPE``), and a translucent window fights the
``windows11`` style, which draws its own opaque control and card backgrounds.
The result is a window where some surfaces are tinted by the desktop and
others are not, which looks less finished than a plain, consistent surface.
The one piece of window chrome worth having is a title bar that matches the
theme; see :meth:`ThemeManager.apply_window_chrome`.
"""

from __future__ import annotations

import ctypes
import sys
import weakref
from enum import StrEnum
from typing import Final, Mapping

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QStyleFactory, QWidget

from app.ui.theme.fonts import font_body
from app.ui.theme.stylesheet import build_qss
from app.ui.theme.tokens import DARK_TOKENS, LIGHT_TOKENS

#: The Qt style that draws Windows 11 controls. Present from Qt 6.7 on
#: Windows 11; absent on Windows 10 and on the offscreen platform, where the
#: default style (``windowsvista`` / ``Fusion``) is left in place.
NATIVE_STYLE_KEY: Final[str] = "windows11"

#: ``DWMWA_USE_IMMERSIVE_DARK_MODE`` from ``dwmapi.h``. 20 on Windows 11 and
#: on Windows 10 20H1+; earlier builds used 19 and are simply not supported
#: here -- a light title bar on an old build is a cosmetic loss, not a fault.
_DWMWA_USE_IMMERSIVE_DARK_MODE: Final[int] = 20


class ThemeMode(StrEnum):
    """What the user chose in Settings. Persisted, so never rename."""

    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"

    @property
    def label(self) -> str:
        return {
            ThemeMode.SYSTEM: "Match Windows",
            ThemeMode.LIGHT: "Light",
            ThemeMode.DARK: "Dark",
        }[self]


class StatusSeverity(StrEnum):
    """How a status reads to the user. Drives badge and dot colours.

    Values are the strings the stylesheet's ``severity`` selector matches, so
    a widget can pass ``severity.value`` straight into ``setProperty``. The
    members mirror :class:`app.core.errors.Severity` and add ``SUCCESS`` and
    ``NEUTRAL``, which are states rather than failures.
    """

    SUCCESS = "success"
    WARNING = "warning"
    BLOCKED = "blocked"
    ERROR = "error"
    INFO = "info"
    NEUTRAL = "neutral"

    @property
    def label(self) -> str:
        return {
            StatusSeverity.SUCCESS: "Done",
            StatusSeverity.WARNING: "Attention",
            StatusSeverity.BLOCKED: "Blocked",
            StatusSeverity.ERROR: "Problem",
            StatusSeverity.INFO: "Info",
            StatusSeverity.NEUTRAL: "Idle",
        }[self]


class ThemeManager(QObject):
    """Owns the application's appearance.

    Construct once, immediately after the :class:`QApplication`, then call
    :meth:`set_mode` with the persisted preference.
    """

    #: Emitted after a new stylesheet is in force, carrying the effective
    #: appearance as ``"light"`` or ``"dark"`` (not the mode: a listener that
    #: wants to redraw a pixmap needs to know what it is drawing onto, and
    #: ``SYSTEM`` does not answer that).
    theme_changed = Signal(str)

    def __init__(self, app: QApplication, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._app = app
        self._mode = ThemeMode.SYSTEM
        # Windows whose title bar follows the theme. Weak so that closing a
        # dialog does not keep it alive for the life of the application.
        self._chrome_windows: weakref.WeakSet[QWidget] = weakref.WeakSet()

        if NATIVE_STYLE_KEY in QStyleFactory.keys():
            app.setStyle(NATIVE_STYLE_KEY)

        app.setFont(font_body())

        style_hints = app.styleHints()
        style_hints.colorSchemeChanged.connect(self._on_color_scheme_changed)

        self._apply()

    # -- state --------------------------------------------------------------

    @property
    def mode(self) -> ThemeMode:
        """The user's choice, which may be :attr:`ThemeMode.SYSTEM`."""
        return self._mode

    def is_dark(self) -> bool:
        """Whether dark colours are currently in force.

        For an explicit mode the answer is the mode itself; only
        :attr:`ThemeMode.SYSTEM` has to ask Qt, and Qt reports
        :attr:`Qt.ColorScheme.Unknown` on platforms that cannot tell (the
        offscreen plugin, for one), which is treated as light.
        """
        if self._mode is ThemeMode.DARK:
            return True
        if self._mode is ThemeMode.LIGHT:
            return False
        return self._system_scheme_is_dark()

    def current_tokens(self) -> Mapping[str, str]:
        """The token set matching the current appearance."""
        return DARK_TOKENS if self.is_dark() else LIGHT_TOKENS

    # -- commands -----------------------------------------------------------

    def set_mode(self, mode: ThemeMode) -> None:
        """Switch appearance and re-apply the stylesheet.

        Always re-applies, even when ``mode`` is unchanged, so this doubles as
        the recovery path if something has called ``setStyleSheet`` on the
        application behind our back.
        """
        self._mode = ThemeMode(mode)
        scheme = {
            ThemeMode.LIGHT: Qt.ColorScheme.Light,
            ThemeMode.DARK: Qt.ColorScheme.Dark,
            # Unknown is not "no opinion at startup": it is the documented way
            # to resume following the system setting.
            ThemeMode.SYSTEM: Qt.ColorScheme.Unknown,
        }[self._mode]
        self._app.styleHints().setColorScheme(scheme)
        self._apply()

    def apply_window_chrome(self, widget: QWidget) -> None:
        """Make ``widget``'s native title bar follow the theme.

        Qt styles the client area; the title bar belongs to DWM and stays
        light unless asked. Without this, a dark window wears a white caption
        bar, which is the single most obvious "this app was not written for
        Windows" tell.

        The widget is remembered (weakly) and updated again on every theme
        change. Everything here degrades silently: on Windows 10 the
        attribute may be rejected, and on any non-Windows platform the call
        is skipped entirely, because a missing title-bar tint must never stop
        a window from opening.
        """
        self._chrome_windows.add(widget)
        self._set_immersive_dark(widget, self.is_dark())

    # -- internals ----------------------------------------------------------

    def _system_scheme_is_dark(self) -> bool:
        hints = QGuiApplication.styleHints()
        return bool(hints) and hints.colorScheme() == Qt.ColorScheme.Dark

    def _apply(self) -> None:
        """Rebuild the stylesheet, refresh chrome and announce the change."""
        dark = self.is_dark()
        self._app.setStyleSheet(build_qss(self.current_tokens()))
        for widget in list(self._chrome_windows):
            self._set_immersive_dark(widget, dark)
        self.theme_changed.emit("dark" if dark else "light")

    def _on_color_scheme_changed(self, _scheme: Qt.ColorScheme) -> None:
        """React to the OS switching appearance.

        Deferred by one event-loop pass on purpose: at the moment this signal
        fires Qt has not finished installing the new palette, so re-applying
        synchronously would style the window for the appearance it is leaving.
        """
        if self._mode is not ThemeMode.SYSTEM:
            # An explicit choice wins. Qt should not be reporting a change at
            # all in that case, but a stray notification must not override
            # what the user asked for.
            return
        QTimer.singleShot(0, self._apply)

    def _set_immersive_dark(self, widget: QWidget, dark: bool) -> None:
        if not sys.platform.startswith("win"):
            return
        try:
            handle = int(widget.winId())
            if not handle:
                return
            value = ctypes.c_int(1 if dark else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(  # type: ignore[attr-defined]
                ctypes.c_void_p(handle),
                ctypes.c_uint(_DWMWA_USE_IMMERSIVE_DARK_MODE),
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
        except (AttributeError, OSError, RuntimeError, ValueError):
            # No dwmapi, an unsupported attribute on this build, or a widget
            # with no native window yet. All cosmetic; carry on.
            return
