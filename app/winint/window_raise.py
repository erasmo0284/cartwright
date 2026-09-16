"""Bringing the window to the front when the user asks for it.

Windows deliberately stops a background process from stealing the foreground:
a process that is not the active one, and has had no recent input from the
user, cannot simply call ``SetForegroundWindow``. The call returns without
raising anything and Windows flashes the taskbar button instead. That is why
``show()`` plus ``activateWindow()`` on their own usually leave a minimised
window flashing in the taskbar rather than appearing in front of the user,
which looks broken when they have just double-clicked the tray icon or
launched a second copy expressly to get the window back.

The accepted way out is to borrow the foreground thread's input queue with
``AttachThreadInput``, which makes Windows treat this process as part of the
active input context for the duration, raise the window, and then detach
again. Every step is optional: if any of it fails the window has still been
shown and raised through Qt, so the user sees a flashing taskbar button in
the worst case rather than an error.
"""

from __future__ import annotations

import sys
from typing import Any

from app.diagnostics.logger import get_logger

logger = get_logger("winint.window_raise")


def show_and_raise(widget: Any) -> None:
    """Show ``widget``, then do everything possible to put it in front.

    Accepts any ``QWidget``. Never raises: failing to reach the foreground is
    a cosmetic problem and must not interrupt whatever asked for the window.
    """
    if widget is None:  # pragma: no cover - defensive
        return

    try:
        if widget.isMinimized() or not widget.isVisible():
            widget.showNormal()
        else:
            widget.show()
        widget.raise_()
        widget.activateWindow()
    except Exception:  # pragma: no cover - defensive
        logger.debug("Showing the window through Qt failed", exc_info=True)
        return

    _force_foreground(widget)


def _force_foreground(widget: Any) -> None:
    """The Windows-specific part. Silently does nothing anywhere else."""
    if sys.platform != "win32":
        return

    import ctypes

    attached = False
    foreground_thread = 0
    our_thread = 0
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        user32.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
        user32.BringWindowToTop.argtypes = (ctypes.c_void_p,)

        handle = ctypes.c_void_p(int(widget.winId()))
        foreground = user32.GetForegroundWindow()
        our_thread = kernel32.GetCurrentThreadId()
        foreground_thread = (
            user32.GetWindowThreadProcessId(ctypes.c_void_p(foreground), None)
            if foreground
            else 0
        )

        if foreground_thread and foreground_thread != our_thread:
            attached = bool(user32.AttachThreadInput(foreground_thread, our_thread, True))

        user32.BringWindowToTop(handle)
        user32.SetForegroundWindow(handle)
    except Exception:
        logger.debug("Could not bring the window to the front", exc_info=True)
    finally:
        if attached:
            try:
                ctypes.WinDLL("user32", use_last_error=True).AttachThreadInput(
                    foreground_thread, our_thread, False
                )
            except Exception:  # pragma: no cover - defensive
                logger.debug("Detaching from the foreground thread failed", exc_info=True)
