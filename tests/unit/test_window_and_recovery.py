"""Tests for the window's own behaviour and for recovery from bad data.

These cover the four ways the shell could lie to the user or lose them:

* an inline hint under an input that shows an error in the same grey as
  ordinary guidance, so "that link was not an Amazon product" reads as
  advice;
* a close that is accepted after the user said *not* to stop a purchase,
  which makes the window vanish while the process keeps running;
* a second launch that cannot raise the first instance's window, which is
  the only way back into an app that started in the tray;
* a damaged database that reaches the crash handler as a raw ``sqlite3``
  error instead of a sentence and the offer of a backup.

Everything runs under the offscreen platform. No window is shown.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtGui import QCloseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.config import CloseButtonAction  # noqa: E402
from app.core.errors import AppError, ErrorCode  # noqa: E402
from app.database.database import Database  # noqa: E402
from app.paths import AppPaths  # noqa: E402
from app.ui import main_window as window_module  # noqa: E402
from app.ui.app_context import AppContext  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.purchase.new_purchase_page import NewPurchasePage  # noqa: E402
from app.ui.theme import StatusSeverity, ThemeManager  # noqa: E402
from app.ui.theme.stylesheet import build_qss  # noqa: E402


@pytest.fixture(scope="session")
def qt_app() -> Iterator[QApplication]:
    app = QApplication.instance() or QApplication([])
    yield app  # type: ignore[misc]


@pytest.fixture
def context(app_paths: AppPaths, qt_app: QApplication) -> Iterator[AppContext]:
    """A real context, never started: ``start()`` would launch Chromium."""
    built = AppContext(paths=app_paths)
    yield built
    built.database.close_all()


# ---------------------------------------------------------------------------
# The inline hint under the product link field
# ---------------------------------------------------------------------------


class TestInlineHintSeverity:
    def test_an_error_hint_is_not_styled_as_ordinary_guidance(
        self, context: AppContext
    ) -> None:
        """The severity has to reach the widget, or the colour cannot follow.

        Both branches of this once set ``role="caption"``, so an error under
        the field was drawn in the same muted grey as "this only reads the
        page".
        """
        page = NewPurchasePage(context.settings)
        hint = page._input_hint  # noqa: SLF001 - asserting on the rendered widget

        assert hint.objectName() == "InlineHint"
        assert hint.property("severity") == StatusSeverity.NEUTRAL.value

        page.show_failure(
            AppError(
                ErrorCode.PRODUCT_NOT_FOUND,
                context={"reason": "not_a_product"},
            )
        )
        assert hint.property("severity") == StatusSeverity.ERROR.value
        assert "Problem" in hint.accessibleDescription()

        page.show_cancelled()
        assert hint.property("severity") == StatusSeverity.NEUTRAL.value

    def test_every_severity_has_a_rule_in_the_stylesheet(self) -> None:
        """A severity with no rule would silently fall back to muted grey."""
        for mode in ("light", "dark"):
            from app.ui.theme.tokens import DARK_TOKENS, LIGHT_TOKENS

            sheet = build_qss(LIGHT_TOKENS if mode == "light" else DARK_TOKENS)
            for severity in StatusSeverity:
                assert f'#InlineHint[severity="{severity.value}"]' in sheet, (
                    f"{severity.value} has no InlineHint rule in {mode} mode"
                )


# ---------------------------------------------------------------------------
# Closing the window
# ---------------------------------------------------------------------------


class _FakeGuard(QObject):
    """Stands in for the single-instance guard."""

    activate_requested = Signal(object)


class TestClosing:
    def _window(self, context: AppContext, qt_app: QApplication, guard=None):
        theme = ThemeManager(qt_app)
        return MainWindow(context, theme, guard=guard)

    def test_declining_to_stop_a_purchase_keeps_the_window(
        self, context: AppContext, qt_app: QApplication, monkeypatch
    ) -> None:
        """Saying "no" to "Close anyway" must not close the window.

        Accepting the close here hid the window while the process kept
        running -- with no tray icon, since this is the exit path -- leaving
        the user no way back in and no way to stop the purchase.
        """
        context.settings.update(close_button_action=CloseButtonAction.EXIT)
        window = self._window(context, qt_app)
        monkeypatch.setattr(window, "_busy_with_a_purchase", lambda: True)
        monkeypatch.setattr(window_module, "confirm", lambda *a, **k: False)

        event = QCloseEvent()
        window.closeEvent(event)

        assert not event.isAccepted(), "the close must be refused"
        assert not window._really_quitting  # noqa: SLF001

    def test_confirming_closes_and_quits(
        self, context: AppContext, qt_app: QApplication, monkeypatch
    ) -> None:
        context.settings.update(close_button_action=CloseButtonAction.EXIT)
        window = self._window(context, qt_app)
        monkeypatch.setattr(window, "_busy_with_a_purchase", lambda: True)
        monkeypatch.setattr(window_module, "confirm", lambda *a, **k: True)
        quit_calls: list[int] = []
        monkeypatch.setattr(QApplication, "quit", lambda *a: quit_calls.append(1))

        event = QCloseEvent()
        window.closeEvent(event)

        assert event.isAccepted()
        assert window._really_quitting  # noqa: SLF001
        assert quit_calls, "the application should have been asked to quit"


class TestSecondLaunch:
    def test_a_tray_start_can_still_be_raised_by_a_second_launch(
        self, context: AppContext, qt_app: QApplication, monkeypatch
    ) -> None:
        """Starting hidden is exactly when this signal matters most.

        The connection used to be made after an early ``return`` taken by the
        ``--tray`` path, so double-clicking the shortcut again did nothing at
        all on the one start where the window is not already on screen.
        """
        guard = _FakeGuard()
        theme = ThemeManager(qt_app)
        window = MainWindow(context, theme, guard=guard)
        opened: list[int] = []
        monkeypatch.setattr(window, "_on_tray_open", lambda: opened.append(1))

        window.prepare(start_in_tray=True)
        guard.activate_requested.emit(None)

        assert opened, "a second launch must be able to raise the window"


# ---------------------------------------------------------------------------
# A damaged database
# ---------------------------------------------------------------------------


class TestDamagedDatabase:
    def test_a_corrupt_file_is_reported_as_an_app_error(self, tmp_path: Path) -> None:
        """Not a raw ``sqlite3.DatabaseError`` from the crash handler.

        The user can act on this one -- it is what the rolling backups are
        for -- so it has to arrive as a typed error with wording, not as a
        traceback.
        """
        corrupt = tmp_path / "app.db"
        corrupt.write_bytes(b"this is not a database at all" * 64)
        database = Database(corrupt, backups_dir=tmp_path / "backups")
        try:
            with pytest.raises(AppError) as excinfo:
                database.migrate()
        finally:
            database.close_all()
        assert excinfo.value.code is ErrorCode.DATABASE_ERROR
        assert isinstance(excinfo.value.__cause__, sqlite3.Error)

    def test_a_directory_in_place_of_the_file_is_reported_too(
        self, tmp_path: Path
    ) -> None:
        blocked = tmp_path / "app.db"
        blocked.mkdir()
        database = Database(blocked, backups_dir=tmp_path / "backups")
        with pytest.raises(AppError) as excinfo:
            database.connection()
        assert excinfo.value.code is ErrorCode.DATABASE_ERROR

    def test_startup_offers_the_newest_backup_and_keeps_the_damaged_file(
        self, app_paths: AppPaths, qt_app: QApplication, monkeypatch
    ) -> None:
        """The recovery path restores, starts, and does not destroy evidence."""
        from PySide6.QtWidgets import QMessageBox

        # A good database, backed up, then damaged.
        database = Database(
            app_paths.database_file, backups_dir=app_paths.backups_dir
        )
        database.migrate()
        backup = database.backup(reason="test")
        database.close_all()
        assert backup is not None and backup.exists()
        app_paths.database_file.write_bytes(b"corrupted" * 128)

        monkeypatch.setattr(
            QMessageBox,
            "question",
            classmethod(lambda cls, *a, **k: QMessageBox.StandardButton.Yes),
        )
        error = AppError(ErrorCode.DATABASE_ERROR, context={"reason": "unreadable"})
        recovered = window_module_main_recovery(app_paths, error)
        assert recovered is not None
        try:
            assert recovered.database.current_version() > 0
        finally:
            recovered.database.close_all()

        damaged = app_paths.database_file.with_name(
            app_paths.database_file.name + ".damaged"
        )
        assert damaged.exists(), "the damaged file must be kept for diagnosis"

    def test_declining_the_restore_does_not_touch_the_data(
        self, app_paths: AppPaths, qt_app: QApplication, monkeypatch
    ) -> None:
        from PySide6.QtWidgets import QMessageBox

        database = Database(
            app_paths.database_file, backups_dir=app_paths.backups_dir
        )
        database.migrate()
        assert database.backup(reason="test") is not None
        database.close_all()
        app_paths.database_file.write_bytes(b"corrupted" * 128)
        before = app_paths.database_file.read_bytes()

        monkeypatch.setattr(
            QMessageBox,
            "question",
            classmethod(lambda cls, *a, **k: QMessageBox.StandardButton.No),
        )
        error = AppError(ErrorCode.DATABASE_ERROR, context={"reason": "unreadable"})
        assert window_module_main_recovery(app_paths, error) is None
        assert app_paths.database_file.read_bytes() == before

    def test_a_failure_with_no_backup_is_shown_not_restored(
        self, app_paths: AppPaths, qt_app: QApplication, monkeypatch
    ) -> None:
        from PySide6.QtWidgets import QMessageBox

        shown: list[str] = []
        monkeypatch.setattr(
            QMessageBox,
            "critical",
            classmethod(lambda cls, _parent, _title, text, *a, **k: shown.append(text)),
        )
        asked: list[int] = []
        monkeypatch.setattr(
            QMessageBox,
            "question",
            classmethod(lambda cls, *a, **k: asked.append(1)),
        )
        error = AppError(ErrorCode.BROWSER_UNAVAILABLE, context={})
        assert window_module_main_recovery(app_paths, error) is None
        assert shown and not asked, "a non-data failure must not offer a restore"


class TestRecoveryEndToEnd:
    """The whole startup path, from a damaged file to a started context.

    The other recovery tests construct the error themselves. This one lets
    ``AppContext`` fail for real, which is the only way to catch the Windows
    trap in the middle: a database whose connection is still open cannot be
    moved aside, so the restore fails for a second, unrelated reason.
    """

    def test_a_corrupt_database_is_recovered_at_startup(
        self, app_paths: AppPaths, qt_app: QApplication, monkeypatch
    ) -> None:
        from PySide6.QtWidgets import QMessageBox

        database = Database(
            app_paths.database_file, backups_dir=app_paths.backups_dir
        )
        database.migrate()
        assert database.backup(reason="test") is not None
        database.close_all()
        app_paths.database_file.write_bytes(b"corrupted" * 256)

        with pytest.raises(AppError) as excinfo:
            AppContext(paths=app_paths)
        assert excinfo.value.code is ErrorCode.DATABASE_ERROR

        monkeypatch.setattr(
            QMessageBox,
            "question",
            classmethod(lambda cls, *a, **k: QMessageBox.StandardButton.Yes),
        )
        recovered = window_module_main_recovery(app_paths, excinfo.value)
        assert recovered is not None, "the restore must succeed"
        try:
            assert recovered.database.current_version() > 0
            assert recovered.settings.current.test_mode is True
        finally:
            recovered.database.close_all()


def window_module_main_recovery(paths: AppPaths, error: AppError):
    """Call the private startup helper without importing it at module scope.

    ``app.main`` sets environment variables and installs hooks at import
    time in other code paths, so it is imported inside the test.
    """
    from app.main import _offer_database_recovery

    return _offer_database_recovery(paths, error)
