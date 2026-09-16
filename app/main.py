"""Application entry point.

The order of operations here is deliberate and fragile in the sense that
getting it wrong produces symptoms that look like something else entirely:

1. **Crash capture first.** A ``--windowed`` PyInstaller build has no console,
   so ``sys.stdout`` and ``sys.stderr`` can be ``None`` and every traceback
   vanishes silently. The harness is installed before anything else is
   imported, so even a failure while importing Qt lands in a log file.
2. **Playwright's browser path before Playwright is imported.** The value is
   read by the Node driver process that Playwright spawns, and it must match
   between install time and run time.
3. **The AUMID before any window exists.** Windows groups taskbar entries and
   attributes toast notifications by it, and setting it after a window has
   been created is too late.
4. **The high-DPI rounding policy before ``QApplication``.** It cannot be
   changed afterwards.
5. **Single-instance check before building anything expensive.** A second
   launch should raise the first window and exit, not start a second browser
   against the same profile -- Chromium refuses to share a profile, so the
   second copy would fail in a confusing way.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # Imported for typing only: the runtime import order in
    # ``main`` is deliberate and must not be disturbed by a module-level one.
    from app.core.errors import AppError
    from app.paths import AppPaths
    from app.ui.app_context import AppContext

# --------------------------------------------------------------------------
# Step 1: crash capture, before any other import that could fail.
# --------------------------------------------------------------------------


def _install_crash_harness() -> Path:
    """Make sure a failure is written down somewhere, and return the log dir."""
    import faulthandler

    from app.paths import get_paths

    paths = get_paths().ensure()
    logs_dir = paths.logs_dir

    # In a windowed build these are None, and any write to them raises.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(  # noqa: SIM115
            logs_dir / "stderr.log", "a", buffering=1, encoding="utf-8"
        )

    # Native crashes inside Qt or shiboken never reach Python's excepthook.
    handle = open(  # noqa: SIM115
        logs_dir / "faulthandler.log", "a", buffering=1, encoding="utf-8"
    )
    faulthandler.enable(file=handle, all_threads=True)
    return logs_dir


def _install_exception_hooks() -> None:
    """Route unhandled exceptions to the log, and show them once to the user."""
    import threading

    logger = logging.getLogger("app.crash")
    already_shown = {"value": False}

    def show(exc_type: type[BaseException], exc: BaseException, tb: object) -> None:
        logger.critical("Unhandled exception", exc_info=(exc_type, exc, tb))  # type: ignore[arg-type]
        if already_shown["value"]:
            return
        already_shown["value"] = True
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            from app.branding import BRAND
            from app.paths import get_paths

            if QApplication.instance() is None:
                return
            detail = "".join(
                traceback.format_exception(exc_type, exc, tb)  # type: ignore[arg-type]
            )
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle(f"{BRAND.display_name} ran into a problem")
            box.setText(
                "Something went wrong inside the app. No order was placed by "
                "this failure."
            )
            box.setInformativeText(
                f"The details were written to:\n{get_paths().logs_dir}"
            )
            box.setDetailedText(detail[-4000:])
            box.exec()
        except Exception:  # noqa: BLE001 - never fail inside a crash handler
            pass

    sys.excepthook = show

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        logger.critical(
            "Unhandled exception in a background thread",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),  # type: ignore[arg-type]
        )

    threading.excepthook = thread_hook


def _install_qt_message_handler() -> None:
    """Send Qt's own warnings to the log instead of a console nobody sees."""
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    logger = logging.getLogger("app.qt")
    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def handler(mode: QtMsgType, context: object, message: str) -> None:
        logger.log(levels.get(mode, logging.INFO), "Qt: %s", message)

    qInstallMessageHandler(handler)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def _parse_arguments(argv: list[str]) -> argparse.Namespace:
    from app.branding import BRAND

    parser = argparse.ArgumentParser(
        prog=BRAND.exe_name,
        description=f"{BRAND.display_name} - {BRAND.tagline}",
    )
    parser.add_argument(
        "--tray",
        action="store_true",
        help="start hidden in the system tray (used when starting with Windows)",
    )
    parser.add_argument(
        "--minimized",
        action="store_true",
        help="alias for --tray",
    )
    parser.add_argument(
        "--reset-window",
        action="store_true",
        help="ignore the saved window position and size",
    )
    parser.add_argument(
        "--version", action="store_true", help="print the version and exit"
    )
    # Unknown arguments are ignored rather than fatal: a stale shortcut from
    # an older version must not stop the app from starting.
    namespace, unknown = parser.parse_known_args(argv)
    if unknown:
        logging.getLogger("app.main").info(
            "Ignoring unrecognised arguments", extra={"arguments": unknown}
        )
    return namespace


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def _offer_database_recovery(
    paths: "AppPaths", error: "AppError"
) -> "AppContext | None":
    """Report a failed start, and offer a backup when the data is at fault.

    A damaged or unreadable database is the one startup failure the user can
    actually fix, and the app keeps rolling backups for exactly this case.
    Returns a started context, or ``None`` when the app cannot continue.
    """
    from PySide6.QtWidgets import QMessageBox

    from app.branding import BRAND
    from app.core.errors import AppError, ErrorCode
    from app.database.database import restore_backup
    from app.ui.app_context import AppContext

    logger = logging.getLogger("app.main")
    title = f"{BRAND.display_name} cannot start"
    is_database = isinstance(error, AppError) and error.code is ErrorCode.DATABASE_ERROR
    backups = (
        sorted(
            paths.backups_dir.glob("app-*.db"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if is_database and paths.backups_dir.exists()
        else []
    )

    detail = getattr(error, "detail", str(error))
    if not backups:
        QMessageBox.critical(
            None, title, f"{getattr(error, 'title', 'Startup failed')}\n\n{detail}"
        )
        return None

    newest = backups[0]
    answer = QMessageBox.question(
        None,
        title,
        f"{detail}\n\n"
        f"A backup from {_file_stamp(newest)} is available. Restore it and "
        "start?\n\nAnything recorded after that backup will be lost. Your "
        "Amazon account and your real orders are not affected.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if answer != QMessageBox.StandardButton.Yes:
        return None

    try:
        # The damaged file is kept, not deleted: it is the only copy of
        # whatever was written after the backup.
        damaged = paths.database_file.with_name(paths.database_file.name + ".damaged")
        damaged.unlink(missing_ok=True)
        paths.database_file.replace(damaged)
        restore_backup(newest, paths.database_file)
        logger.warning(
            "Restored the database from a backup",
            extra={"backup": newest.name},
        )
        return AppContext(paths=paths)
    except (AppError, OSError) as second:
        logger.error("Recovery from a backup failed", exc_info=second)
        QMessageBox.critical(
            None,
            title,
            "Restoring the backup did not work either.\n\n"
            f"{getattr(second, 'detail', str(second))}",
        )
        return None


def _file_stamp(path: Path) -> str:
    """A plain-English timestamp for a file, for use in a dialog."""
    from datetime import datetime

    try:
        moment = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:  # pragma: no cover - the file was just listed
        return "an earlier session"
    return moment.strftime("%d %b %Y at %H:%M")


def main(argv: list[str] | None = None) -> int:
    """Start the application. Returns the process exit code."""
    arguments = _parse_arguments(list(argv if argv is not None else sys.argv[1:]))

    from app.version import BUILD_CHANNEL, VERSION

    if arguments.version:
        from app.branding import BRAND

        print(f"{BRAND.display_name} {VERSION} ({BUILD_CHANNEL})")  # noqa: T201
        return 0

    logs_dir = _install_crash_harness()

    # Step 2: Playwright's browser directory, before Playwright is imported.
    from app.automation.browser_manager import configure_browsers_path
    from app.diagnostics.logger import setup_logging
    from app.paths import get_paths

    paths = get_paths().ensure()
    configure_browsers_path(paths)
    setup_logging(logs_dir)
    _install_exception_hooks()

    logger = logging.getLogger("app.main")
    logger.info(
        "Launching",
        extra={
            "version": VERSION,
            "channel": BUILD_CHANNEL,
            "tray": bool(arguments.tray or arguments.minimized),
            "frozen": bool(getattr(sys, "frozen", False)),
        },
    )

    # Step 3: the AUMID, before any window is created.
    from app.winint.aumid import register_aumid, set_process_aumid

    set_process_aumid()
    try:
        register_aumid()
    except Exception:  # noqa: BLE001 - notifications degrade, the app still runs
        logger.warning("Could not register for Windows notifications", exc_info=True)

    # Step 4: the high-DPI policy, before QApplication.
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    from PySide6.QtWidgets import QApplication, QMessageBox

    from app.branding import BRAND

    app = QApplication(sys.argv[:1])
    app.setApplicationName(BRAND.qt_application)
    app.setOrganizationName(BRAND.qt_organization)
    app.setApplicationDisplayName(BRAND.display_name)
    app.setApplicationVersion(VERSION)
    # Closing the window must not end the process while monitoring runs.
    app.setQuitOnLastWindowClosed(False)

    _install_qt_message_handler()

    from app.ui.theme import ThemeManager, app_icon

    app.setWindowIcon(app_icon())

    # Step 5: single instance. A second launch raises the first window.
    from app.winint.single_instance import SingleInstance

    guard = SingleInstance(BRAND.single_instance_key, parent=app)
    if not guard.acquire(payload="activate"):
        if guard.last_error:
            logger.warning(
                "Another copy is running but did not respond",
                extra={"detail": guard.last_error},
            )
            QMessageBox.information(
                None,
                BRAND.display_name,
                f"{BRAND.display_name} appears to be running already but is "
                "not responding. Check the system tray, or sign out and back "
                "in to clear it.",
            )
        else:
            logger.info("Another copy is already running; asked it to show itself")
        return 0

    theme = ThemeManager(app)

    from app.core.errors import AppError
    from app.ui.app_context import AppContext

    try:
        context = AppContext(paths=paths)
    except AppError as error:
        logger.error("Could not start", exc_info=error)
        context = _offer_database_recovery(paths, error)
        if context is None:
            return 1

    from app.config import ThemePreference
    from app.ui.main_window import MainWindow

    theme.set_mode(_theme_mode(context.settings.current.theme))

    window = MainWindow(context, theme, guard=guard)
    theme.apply_window_chrome(window)

    start_in_tray = bool(arguments.tray or arguments.minimized)
    window.prepare(start_in_tray=start_in_tray)

    app.aboutToQuit.connect(context.shutdown)

    uncertain = context.start()
    if uncertain:
        window.raise_uncertain_purchases(uncertain)

    exit_code = app.exec()
    logger.info("Exited", extra={"exit_code": exit_code})
    return int(exit_code)


def _theme_mode(preference: object) -> object:
    from app.config import ThemePreference
    from app.ui.theme import ThemeMode

    return {
        ThemePreference.SYSTEM: ThemeMode.SYSTEM,
        ThemePreference.LIGHT: ThemeMode.LIGHT,
        ThemePreference.DARK: ThemeMode.DARK,
    }.get(preference, ThemeMode.SYSTEM)  # type: ignore[arg-type]


if __name__ == "__main__":
    raise SystemExit(main())
