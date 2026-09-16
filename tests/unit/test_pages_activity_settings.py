"""Tests for the Activity and Settings screens.

These two screens are the ones a user goes to when something has gone wrong,
so the tests assert on what they would actually read, not merely that the
widgets were constructed. Four of them exist for safety rather than tidiness:

* no visible label may contain a stored ``error_code`` verbatim -- an internal
  identifier in an activity row reads to a person as a crash;
* the activity feed must page, because the database keeps five thousand rows
  and building five thousand widgets would freeze the window;
* turning test mode *off* must be impossible without an explicit confirmation,
  since that switch is the difference between a rehearsal and real money;
* no page module may call ``setStyleSheet``, which would take the screen out
  of the native Windows 11 drawing and out of the theme.

A real :class:`AppContext` is used rather than a stub, so the pages are
exercised against the actual settings service, repositories and health checks.
It is never started: :meth:`AppContext.start` would launch the browser thread
and a real Chromium process.

Everything runs under the offscreen platform, set before the first
:class:`QApplication` exists because Qt reads it exactly once at that point.
Nothing may open a window.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

from app.config import NotificationKind  # noqa: E402
from app.core.money import Money  # noqa: E402
from app.core.timeutil import format_day, to_iso, utcnow  # noqa: E402
from app.database.database import Database  # noqa: E402
from app.database.records import ActivityCategory, ActivitySeverity  # noqa: E402
from app.paths import AppPaths  # noqa: E402
from app.ui.activity import activity_page as activity_module  # noqa: E402
from app.ui.activity.activity_page import PAGE_SIZE, ActivityPage  # noqa: E402
from app.ui.app_context import AppContext  # noqa: E402
from app.ui.components import EmptyState  # noqa: E402
from app.ui.settings import auto_buy_dialog as dialog_module  # noqa: E402
from app.ui.settings import settings_page as settings_module  # noqa: E402
from app.ui.settings.auto_buy_dialog import (  # noqa: E402
    ALWAYS_CHECKED,
    AutoBuyConsentDialog,
)
from app.ui.settings.settings_page import SECTION_ORDER, SettingsPage  # noqa: E402
from app.ui.theme import ThemeManager  # noqa: E402

#: A code that is in the catalogue, and one that is not. Both must stay out of
#: the interface: the first because it has human wording to show instead, the
#: second because there is nothing to show at all.
KNOWN_ERROR_CODE = "login_expired"
UNKNOWN_ERROR_CODE = "gremlins_in_the_wiring"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qt_app() -> Iterator[QApplication]:
    """A single offscreen :class:`QApplication` for the whole module.

    Qt permits only one application object per process, so this is reused
    rather than recreated, and it is never destroyed: tearing it down while
    other Qt objects are alive crashes the interpreter.
    """
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    yield app


@pytest.fixture(scope="module")
def theme(qt_app: QApplication) -> ThemeManager:
    """One theme manager for the module; it owns the application stylesheet."""
    return ThemeManager(qt_app)


@pytest.fixture
def context(
    qt_app: QApplication, app_paths: AppPaths, database: Database
) -> Iterator[AppContext]:
    """A real application context, with nothing running behind it.

    The ``database`` fixture is requested for its side effect: it migrates the
    schema *before* :class:`AppContext` turns file logging on, which keeps the
    migration quiet. The root logger's level and handlers are then restored,
    because ``setup_logging`` reconfigures the process-wide root logger and
    every later test in the session would otherwise inherit it -- along with
    handlers pointed at a temporary directory that is about to be deleted.
    """
    root = logging.getLogger()
    level = root.level
    handlers = list(root.handlers)

    built = AppContext(paths=app_paths)
    try:
        yield built
    finally:
        built.shutdown()
        root.setLevel(level)
        root.handlers[:] = handlers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def label_texts(widget: QWidget) -> list[str]:
    """Every piece of text the widget tree would put on screen."""
    return [label.text() for label in widget.findChildren(QLabel)]


def seed(
    context: AppContext,
    *,
    category: ActivityCategory,
    severity: ActivitySeverity = ActivitySeverity.INFO,
    title: str = "Something happened",
    **extra: object,
) -> int:
    return context.repositories.activity.add(
        category=category, severity=severity, title=title, **extra
    )


def backdate(context: AppContext, event_id: int, *, days: int) -> None:
    """Move a stored event into the past so day grouping has work to do."""
    moment = to_iso(utcnow() - timedelta(days=days))
    with context.database.transaction() as conn:
        conn.execute(
            "UPDATE activity_events SET created_at = ? WHERE id = ?",
            (moment, event_id),
        )


def seed_mixed_feed(context: AppContext) -> None:
    """Nine events spread over every category the screen can filter on."""
    for index in range(3):
        seed(
            context,
            category=ActivityCategory.PURCHASE,
            severity=ActivitySeverity.SUCCESS,
            title=f"Order placed {index}",
            amount=Money(129_900, "USD"),
        )
    for index in range(2):
        seed(
            context,
            category=ActivityCategory.WATCH_CHECK,
            title=f"Checked the price {index}",
            detail="The price has not changed.",
        )
    seed(
        context,
        category=ActivityCategory.WARNING,
        severity=ActivitySeverity.WARNING,
        title="The seller changed",
    )
    seed(
        context,
        category=ActivityCategory.ERROR,
        severity=ActivitySeverity.ERROR,
        title="The check could not finish",
        error_code=UNKNOWN_ERROR_CODE,
    )
    seed(
        context,
        category=ActivityCategory.ACCOUNT,
        severity=ActivitySeverity.WARNING,
        title="Amazon asked for a sign-in",
        error_code=KNOWN_ERROR_CODE,
    )
    seed(context, category=ActivityCategory.SYSTEM, title="The program started")


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_the_activity_page_builds_with_a_usable_size(
        self, context: AppContext
    ) -> None:
        page = ActivityPage(context)
        hint = page.sizeHint()
        assert hint.isValid()
        assert hint.width() > 0 and hint.height() > 0

    def test_the_settings_page_builds_with_a_usable_size(
        self, context: AppContext, theme: ThemeManager
    ) -> None:
        page = SettingsPage(context, theme)
        hint = page.sizeHint()
        assert hint.isValid()
        assert hint.width() > 0 and hint.height() > 0

    def test_no_page_module_sets_its_own_stylesheet(self) -> None:
        """A widget's own stylesheet beats the theme and never follows it."""
        modules = (
            activity_module.__file__,
            settings_module.__file__,
            dialog_module.__file__,
        )
        for path in modules:
            assert path is not None
            source = Path(path).read_text(encoding="utf-8")
            assert "setStyleSheet" not in source, path


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


class TestActivityFeed:
    def test_every_filter_shows_only_its_own_category(
        self, context: AppContext
    ) -> None:
        seed_mixed_feed(context)
        page = ActivityPage(context)

        assert len(page.visible_events()) == 9

        for category, expected in (
            (ActivityCategory.PURCHASE, 3),
            (ActivityCategory.WATCH_CHECK, 2),
            (ActivityCategory.WARNING, 1),
            (ActivityCategory.ERROR, 1),
            (ActivityCategory.ACCOUNT, 1),
        ):
            page.set_filter(category)
            shown = page.visible_events()
            assert len(shown) == expected, category
            assert {event.category for event in shown} == {category}

        page.set_filter(None)
        assert len(page.visible_events()) == 9

    def test_switching_filters_changes_the_rows_on_screen(
        self, context: AppContext
    ) -> None:
        seed_mixed_feed(context)
        page = ActivityPage(context)

        page.set_filter(ActivityCategory.PURCHASE)
        purchases = label_texts(page)
        assert any("Order placed" in text for text in purchases)
        assert not any("Checked the price" in text for text in purchases)

        page.set_filter(ActivityCategory.WATCH_CHECK)
        checks = label_texts(page)
        assert any("Checked the price" in text for text in checks)
        assert not any("Order placed" in text for text in checks)

    def test_each_filter_button_carries_its_own_count(
        self, context: AppContext
    ) -> None:
        seed_mixed_feed(context)
        page = ActivityPage(context)

        texts = [button.text() for button in page._filter_buttons.values()]
        assert "All (9)" in texts
        assert "Purchases (3)" in texts
        assert "Watch checks (2)" in texts

    def test_rows_are_grouped_under_a_day_heading(
        self, context: AppContext
    ) -> None:
        seed(context, category=ActivityCategory.WATCH_CHECK, title="Today's check")
        older = seed(
            context, category=ActivityCategory.WATCH_CHECK, title="An older check"
        )
        backdate(context, older, days=1)
        much_older = seed(
            context, category=ActivityCategory.WATCH_CHECK, title="An ancient check"
        )
        backdate(context, much_older, days=3)

        page = ActivityPage(context)
        texts = label_texts(page)

        assert "Today" in texts
        assert "Yesterday" in texts
        assert format_day(utcnow() - timedelta(days=3)) in texts

    def test_an_amount_is_shown_with_its_currency_symbol(
        self, context: AppContext
    ) -> None:
        seed(
            context,
            category=ActivityCategory.PURCHASE,
            severity=ActivitySeverity.SUCCESS,
            title="Order placed",
            amount=Money(129_900, "USD"),
        )
        page = ActivityPage(context)
        assert "$1,299.00" in label_texts(page)

    def test_a_stored_error_code_never_reaches_the_screen(
        self, context: AppContext
    ) -> None:
        seed_mixed_feed(context)
        page = ActivityPage(context)

        texts = label_texts(page)
        for text in texts:
            assert KNOWN_ERROR_CODE not in text
            assert UNKNOWN_ERROR_CODE not in text

        # The recognised code is still surfaced, in the wording written for
        # people; the unrecognised one contributes nothing at all.
        assert activity_module.failure_summary(KNOWN_ERROR_CODE) in texts
        assert activity_module.failure_summary(UNKNOWN_ERROR_CODE) is None

    def test_an_empty_feed_explains_itself(self, context: AppContext) -> None:
        page = ActivityPage(context)
        empty = page.findChild(EmptyState)

        assert page.visible_events() == ()
        assert empty is not None
        assert not empty.isHidden()
        assert empty.heading == "No activity yet"
        assert empty.body

    def test_each_filter_has_its_own_empty_state(
        self, context: AppContext
    ) -> None:
        seed(context, category=ActivityCategory.WATCH_CHECK, title="A check")
        page = ActivityPage(context)

        page.set_filter(ActivityCategory.PURCHASE)
        empty = page.findChild(EmptyState)
        assert empty is not None
        assert not empty.isHidden()
        assert empty.heading == "No purchases yet"


class TestActivityPaging:
    def test_only_one_page_of_rows_is_built_at_a_time(
        self, context: AppContext
    ) -> None:
        for index in range(250):
            seed(
                context,
                category=ActivityCategory.WATCH_CHECK,
                title=f"Checked the price {index}",
            )

        page = ActivityPage(context)
        assert len(page.visible_events()) == PAGE_SIZE
        assert not page.more_button.isHidden()

        page.more_button.click()
        assert len(page.visible_events()) == 2 * PAGE_SIZE
        assert not page.more_button.isHidden()

        page.more_button.click()
        assert len(page.visible_events()) == 250
        assert page.more_button.isHidden()

    def test_refreshing_goes_back_to_the_first_page(
        self, context: AppContext
    ) -> None:
        for index in range(150):
            seed(
                context,
                category=ActivityCategory.WATCH_CHECK,
                title=f"Checked the price {index}",
            )
        page = ActivityPage(context)
        page.more_button.click()
        assert len(page.visible_events()) == 150

        page.refresh()
        assert len(page.visible_events()) == PAGE_SIZE


class TestActivityClearing:
    def test_clearing_is_abandoned_when_the_user_says_no(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_mixed_feed(context)
        page = ActivityPage(context)
        monkeypatch.setattr(
            activity_module, "confirm_destructive", lambda *a, **k: False
        )

        page.clear_history()

        assert context.repositories.activity.count() == 9
        assert len(page.visible_events()) == 9

    def test_clearing_empties_the_feed_when_confirmed(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed_mixed_feed(context)
        page = ActivityPage(context)
        monkeypatch.setattr(
            activity_module, "confirm_destructive", lambda *a, **k: True
        )

        page.clear_history()

        assert context.repositories.activity.count() == 0
        assert page.visible_events() == ()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettingsSections:
    def test_every_section_is_present_and_in_order(
        self, context: AppContext, theme: ThemeManager
    ) -> None:
        page = SettingsPage(context, theme)
        assert page.section_titles() == SECTION_ORDER
        for title in SECTION_ORDER:
            assert page.section(title) is not None

    def test_the_about_section_names_the_product_from_the_branding(
        self, context: AppContext, theme: ThemeManager
    ) -> None:
        from app.branding import BRAND

        page = SettingsPage(context, theme)
        about = page.section("About")
        assert about is not None
        texts = label_texts(about)
        assert BRAND.display_name in texts
        assert BRAND.disclaimer in texts


class TestSettingsWriteThrough:
    def test_changing_the_quantity_is_stored(
        self, context: AppContext, theme: ThemeManager
    ) -> None:
        page = SettingsPage(context, theme)
        assert context.settings.current.default_quantity == 1

        page.quantity_field.set_value(4)

        assert context.settings.current.default_quantity == 4

    def test_turning_a_notification_off_is_stored(
        self, context: AppContext, theme: ThemeManager
    ) -> None:
        page = SettingsPage(context, theme)
        kind = NotificationKind.TARGET_PRICE_REACHED

        page.notification_switches[kind].setChecked(False)

        assert context.settings.current.notification_toggles[kind.value] is False
        assert context.settings.current.notification_allowed(kind) is False

    def test_an_always_on_notification_is_shown_on_and_cannot_be_changed(
        self, context: AppContext, theme: ThemeManager
    ) -> None:
        page = SettingsPage(context, theme)
        always_on = [kind for kind in NotificationKind if kind.always_on]
        assert always_on, "the always-on set must not be empty"

        for kind in always_on:
            box = page.notification_switches[kind]
            assert box.isChecked() is True
            assert box.isEnabled() is False
            assert box.toolTip()


class TestTestModeGuard:
    def test_turning_test_mode_off_needs_a_confirmation(
        self,
        context: AppContext,
        theme: ThemeManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        page = SettingsPage(context, theme)
        assert context.settings.current.test_mode is True

        monkeypatch.setattr(
            settings_module, "confirm_destructive", lambda *a, **k: False
        )
        page.test_mode_switch.setChecked(False)

        assert context.settings.current.test_mode is True
        assert page.test_mode_switch.isChecked() is True

        monkeypatch.setattr(
            settings_module, "confirm_destructive", lambda *a, **k: True
        )
        page.test_mode_switch.setChecked(False)

        assert context.settings.current.test_mode is False
        assert page.test_mode_switch.isChecked() is False

    def test_turning_test_mode_back_on_needs_no_confirmation(
        self,
        context: AppContext,
        theme: ThemeManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        context.settings.update(test_mode=False)
        page = SettingsPage(context, theme)
        monkeypatch.setattr(
            settings_module,
            "confirm_destructive",
            lambda *a, **k: pytest.fail("the safe direction must not ask"),
        )

        page.test_mode_switch.setChecked(True)

        assert context.settings.current.test_mode is True


class TestAutomaticPurchasing:
    def test_automatic_is_not_selectable_until_it_is_acknowledged(
        self, context: AppContext, theme: ThemeManager
    ) -> None:
        page = SettingsPage(context, theme)
        assert context.settings.current.auto_buy_acknowledged is False
        assert page.mode_combo.count() == 1

        page.set_auto_buy_acknowledged(True)

        assert context.settings.current.auto_buy_acknowledged is True
        assert page.mode_combo.count() == 2

    def test_withdrawing_consent_puts_the_default_back(
        self, context: AppContext, theme: ThemeManager
    ) -> None:
        from app.purchasing.models import PurchaseMode

        page = SettingsPage(context, theme)
        page.set_auto_buy_acknowledged(True)
        page.mode_combo.setCurrentIndex(
            page.mode_combo.findData(PurchaseMode.AUTOMATIC.value)
        )
        assert context.settings.current.default_purchase_mode is PurchaseMode.AUTOMATIC

        page.set_auto_buy_acknowledged(False)

        assert context.settings.current.auto_buy_acknowledged is False
        assert context.settings.current.default_purchase_mode is PurchaseMode.ASSISTED

    def test_the_page_asks_the_window_to_collect_consent(
        self, context: AppContext, theme: ThemeManager
    ) -> None:
        page = SettingsPage(context, theme)
        asked: list[bool] = []
        page.auto_buy_consent_requested.connect(lambda: asked.append(True))

        page.auto_buy_enable_button.click()

        assert asked == [True]
        # Clicking alone must not have changed anything.
        assert context.settings.current.auto_buy_acknowledged is False


class TestAutoBuyConsentDialog:
    def test_confirming_is_impossible_until_the_box_is_ticked(
        self, qt_app: QApplication
    ) -> None:
        dialog = AutoBuyConsentDialog()

        assert dialog.accepted_consent is False
        assert dialog.confirm_button.isEnabled() is False

        dialog.understand_check.setChecked(True)
        assert dialog.confirm_button.isEnabled() is True

        dialog.understand_check.setChecked(False)
        assert dialog.confirm_button.isEnabled() is False

    def test_cancel_is_the_default_button(self, qt_app: QApplication) -> None:
        dialog = AutoBuyConsentDialog()
        assert dialog.cancel_button.isDefault() is True
        assert dialog.confirm_button.isDefault() is False

    def test_every_promised_check_is_listed_on_screen(
        self, qt_app: QApplication
    ) -> None:
        dialog = AutoBuyConsentDialog()
        texts = label_texts(dialog)

        assert ALWAYS_CHECKED, "the promise must not be an empty list"
        for item in ALWAYS_CHECKED:
            assert any(item in text for text in texts), item

    def test_the_dialog_says_test_mode_still_applies(
        self, qt_app: QApplication
    ) -> None:
        dialog = AutoBuyConsentDialog()
        assert any("test mode" in text.lower() for text in label_texts(dialog))

    def test_consent_is_recorded_only_after_the_box_is_ticked(
        self, qt_app: QApplication
    ) -> None:
        dialog = AutoBuyConsentDialog()
        dialog.understand_check.setChecked(True)
        dialog.confirm_button.click()
        assert dialog.accepted_consent is True
