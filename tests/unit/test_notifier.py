"""Tests for :mod:`app.notifications.notifier`.

Nothing here shows a real notification. ``windows_toasts`` is replaced by
:class:`FakeToastModule`, and the AUMID registration is replaced as well so
the suite never writes to the real registry.

The copy tests are the important ones: they are what stops a future edit to a
notification's wording from putting a delivery address, a payment method or
an order number on a lock screen.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from app.config import NotificationKind, SettingsService
from app.core.money import Money
from app.database.database import Database
from app.database.repositories.activity import ActivityRepository
from app.notifications import notifier as notifier_module
from app.notifications.notifier import (
    CHANNEL_NONE,
    CHANNEL_TOAST,
    CHANNEL_TRAY,
    MAX_BODY_CHARS,
    MAX_TITLE_CHARS,
    METHOD_TOAST,
    METHOD_TRAY,
    SUPPRESSED_ALREADY_SENT,
    SUPPRESSED_NO_CHANNEL,
    SUPPRESSED_TURNED_OFF,
    NotificationRequest,
    Notifier,
)

# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------


class FakeToast:
    """Records the arguments a real ``Toast`` would have been built with."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class FakeToastButton:
    def __init__(self, content: str = "", arguments: str = "", **_: Any) -> None:
        self.content = content
        self.arguments = arguments


class FakeToaster:
    def __init__(self, module: FakeToastModule, application_text: str, aumid: str | None = None) -> None:
        self._module = module
        self.application_text = application_text
        self.aumid = aumid

    def show_toast(self, toast: FakeToast) -> None:
        self._module.shown.append(toast)


class FakeToastModule:
    """Enough of ``windows_toasts`` for the notifier, with nothing on screen."""

    def __init__(self, *, fail_on_show: bool = False) -> None:
        self.shown: list[FakeToast] = []
        self.toasters: list[FakeToaster] = []
        self.fail_on_show = fail_on_show
        self.Toast = FakeToast
        self.ToastButton = FakeToastButton

    def InteractableWindowsToaster(  # noqa: N802 - mirrors the real class name
        self, application_text: str, aumid: str | None = None
    ) -> FakeToaster:
        toaster = FakeToaster(self, application_text, aumid)
        self.toasters.append(toaster)
        if self.fail_on_show:
            toaster.show_toast = _raise_show  # type: ignore[method-assign]
        return toaster


def _raise_show(_toast: object) -> None:
    raise RuntimeError("Windows refused the notification")


class FakeTray:
    """Stands in for :class:`app.winint.tray.TrayController`."""

    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[tuple[str, str, int]] = []
        self.fail = fail

    def show_message(self, title: str, body: str, seconds: int = 10) -> None:
        if self.fail:
            raise RuntimeError("no tray on this desktop")
        self.messages.append((title, body, seconds))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_registry_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the AUMID registration away from the real registry."""
    monkeypatch.setattr(notifier_module, "register_aumid", lambda _icon=None: True)
    monkeypatch.setattr(notifier_module, "set_process_aumid", lambda: True)


@pytest.fixture
def settings(database: Database) -> SettingsService:
    return SettingsService(database)


@pytest.fixture
def activity(database: Database) -> ActivityRepository:
    return ActivityRepository(database)


@pytest.fixture
def toasts(monkeypatch: pytest.MonkeyPatch) -> FakeToastModule:
    """A working toast channel."""
    module = FakeToastModule()
    monkeypatch.setattr(notifier_module, "_load_windows_toasts", lambda: module)
    return module


@pytest.fixture
def no_toasts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A toast channel that always raises, as an unsupported machine would."""

    def _raise() -> Any:
        raise RuntimeError("windows_toasts is not available here")

    monkeypatch.setattr(notifier_module, "_load_windows_toasts", _raise)


def last_row(database: Database) -> dict[str, Any]:
    row = database.query_one(
        "SELECT * FROM notification_events ORDER BY id DESC LIMIT 1"
    )
    assert row is not None
    return dict(row)


def row_count(database: Database) -> int:
    return int(database.query_scalar("SELECT COUNT(*) FROM notification_events") or 0)


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


class TestSettingsAreHonoured:
    def test_a_turned_off_kind_is_not_delivered(
        self,
        database: Database,
        settings: SettingsService,
        activity: ActivityRepository,
        toasts: FakeToastModule,
    ) -> None:
        settings.set_notification(NotificationKind.BACK_IN_STOCK, False)
        notifier = Notifier(settings, activity)

        delivered = notifier.notify(notifier_module.back_in_stock("Kettle", 1))

        assert delivered is False
        assert toasts.shown == []
        row = last_row(database)
        assert row["delivered"] == 0
        assert row["suppressed_why"] == SUPPRESSED_TURNED_OFF
        assert row["delivery_method"] is None

    def test_the_master_switch_silences_an_ordinary_kind(
        self,
        database: Database,
        settings: SettingsService,
        activity: ActivityRepository,
        toasts: FakeToastModule,
    ) -> None:
        settings.update(notifications_enabled=False)
        notifier = Notifier(settings, activity)

        assert notifier.notify(notifier_module.login_expired()) is False
        assert last_row(database)["suppressed_why"] == SUPPRESSED_TURNED_OFF

    def test_the_master_switch_does_not_silence_a_purchase(
        self,
        database: Database,
        settings: SettingsService,
        activity: ActivityRepository,
        toasts: FakeToastModule,
    ) -> None:
        """Money being spent is reported whatever the switches say."""
        settings.update(notifications_enabled=False)
        notifier = Notifier(settings, activity)

        request = notifier_module.purchase_completed("Kettle", Money(1999), None, 4)
        assert notifier.notify(request) is True
        assert last_row(database)["delivered"] == 1


class TestDeduplication:
    def test_the_same_news_is_only_sent_once(
        self,
        database: Database,
        settings: SettingsService,
        activity: ActivityRepository,
        toasts: FakeToastModule,
    ) -> None:
        notifier = Notifier(settings, activity)
        request = notifier_module.back_in_stock("Kettle", 1)

        assert notifier.notify(request) is True
        assert notifier.notify(request) is False

        assert len(toasts.shown) == 1
        assert last_row(database)["suppressed_why"] == SUPPRESSED_ALREADY_SENT

    def test_an_expired_window_lets_it_through_again(
        self,
        settings: SettingsService,
        activity: ActivityRepository,
        toasts: FakeToastModule,
    ) -> None:
        notifier = Notifier(settings, activity)
        request = NotificationRequest(
            kind=NotificationKind.BACK_IN_STOCK,
            title="Back in stock",
            body="Kettle is available to buy again.",
            dedupe_key="back_in_stock:1",
            dedupe_seconds=0,
        )

        assert notifier.notify(request) is True
        assert notifier.notify(request) is True

    @pytest.mark.parametrize(
        "kind",
        [kind for kind in NotificationKind if kind.always_on],
    )
    def test_always_on_kinds_are_never_deduped(
        self,
        kind: NotificationKind,
        settings: SettingsService,
        activity: ActivityRepository,
        toasts: FakeToastModule,
    ) -> None:
        notifier = Notifier(settings, activity)
        request = NotificationRequest(
            kind=kind,
            title="Order placed",
            body="Kettle was bought for $19.99.",
            dedupe_key=f"{kind.value}:4",
            dedupe_seconds=86_400,
        )

        assert notifier.notify(request) is True
        assert notifier.notify(request) is True
        assert len(toasts.shown) == 2

    def test_two_products_do_not_silence_each_other(
        self,
        settings: SettingsService,
        activity: ActivityRepository,
        toasts: FakeToastModule,
    ) -> None:
        first = notifier_module.back_in_stock("Kettle", 1)
        second = notifier_module.back_in_stock("Toaster", 2)
        assert first.dedupe_key != second.dedupe_key

        notifier = Notifier(settings, activity)
        assert notifier.notify(first) is True
        assert notifier.notify(second) is True

    def test_dedupe_keys_carry_the_relevant_identifier(self) -> None:
        assert "7" in notifier_module.purchase_ready("Kettle", Money(1), 7).dedupe_key
        assert "9" in notifier_module.purchase_blocked("Kettle", "no", 9).dedupe_key
        assert (
            notifier_module.monitoring_error("Kettle", "timed out").dedupe_key
            != notifier_module.monitoring_error("Toaster", "timed out").dedupe_key
        )

    def test_a_further_price_drop_is_reported_again(
        self,
        settings: SettingsService,
        activity: ActivityRepository,
        toasts: FakeToastModule,
    ) -> None:
        notifier = Notifier(settings, activity)
        first = notifier_module.target_price_reached(
            "Kettle", Money(9900), Money(10_000), 3
        )
        cheaper = notifier_module.target_price_reached(
            "Kettle", Money(8900), Money(10_000), 3
        )

        assert notifier.notify(first) is True
        assert notifier.notify(cheaper) is True


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


class TestDelivery:
    def test_a_toast_is_used_when_windows_accepts_one(
        self,
        database: Database,
        settings: SettingsService,
        activity: ActivityRepository,
        toasts: FakeToastModule,
    ) -> None:
        notifier = Notifier(settings, activity, tray=FakeTray())

        request = notifier_module.back_in_stock("Kettle", 1)
        assert notifier.notify(request) is True

        row = last_row(database)
        assert row["delivered"] == 1
        assert row["delivery_method"] == METHOD_TOAST
        assert toasts.shown[0].kwargs["text_fields"] == [request.title, request.body]

    def test_toast_actions_become_buttons_carrying_the_payload(
        self,
        settings: SettingsService,
        activity: ActivityRepository,
        toasts: FakeToastModule,
    ) -> None:
        notifier = Notifier(settings, activity)
        request = notifier_module.purchase_ready("Kettle", Money(1999), 7)

        assert notifier.notify(request) is True
        buttons = toasts.shown[0].kwargs["actions"]
        assert [button.content for button in buttons] == list(request.actions)
        assert buttons[0].arguments == request.payload

    def test_it_falls_back_to_the_tray_when_the_toast_path_raises(
        self,
        database: Database,
        settings: SettingsService,
        activity: ActivityRepository,
        no_toasts: None,
    ) -> None:
        tray = FakeTray()
        notifier = Notifier(settings, activity, tray=tray)

        request = notifier_module.back_in_stock("Kettle", 1)
        assert notifier.notify(request) is True

        assert tray.messages[0][0] == request.title
        assert tray.messages[0][1] == request.body
        row = last_row(database)
        assert row["delivered"] == 1
        assert row["delivery_method"] == METHOD_TRAY

    def test_it_falls_back_to_the_tray_when_showing_a_toast_fails(
        self,
        database: Database,
        settings: SettingsService,
        activity: ActivityRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        module = FakeToastModule(fail_on_show=True)
        monkeypatch.setattr(notifier_module, "_load_windows_toasts", lambda: module)
        tray = FakeTray()
        notifier = Notifier(settings, activity, tray=tray)

        assert notifier.notify(notifier_module.back_in_stock("Kettle", 1)) is True
        assert len(tray.messages) == 1
        assert last_row(database)["delivery_method"] == METHOD_TRAY

    def test_a_failed_toast_is_not_left_holding_a_reference(
        self,
        settings: SettingsService,
        activity: ActivityRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        module = FakeToastModule(fail_on_show=True)
        monkeypatch.setattr(notifier_module, "_load_windows_toasts", lambda: module)
        notifier = Notifier(settings, activity, tray=FakeTray())

        notifier.notify(notifier_module.back_in_stock("Kettle", 1))

        assert notifier._toasts == []

    def test_a_delivered_toast_is_held_until_it_is_dismissed(
        self,
        settings: SettingsService,
        activity: ActivityRepository,
        toasts: FakeToastModule,
    ) -> None:
        """A collected toast is a click that never arrives."""
        notifier = Notifier(settings, activity)
        notifier.notify(notifier_module.back_in_stock("Kettle", 1))

        assert len(notifier._toasts) == 1
        toasts.shown[0].kwargs["on_dismissed"](object())
        assert notifier._toasts == []

    def test_nothing_is_delivered_when_there_is_no_channel(
        self,
        database: Database,
        settings: SettingsService,
        activity: ActivityRepository,
        no_toasts: None,
    ) -> None:
        notifier = Notifier(settings, activity, tray=None)

        assert notifier.notify(notifier_module.back_in_stock("Kettle", 1)) is False

        row = last_row(database)
        assert row["delivered"] == 0
        assert row["suppressed_why"] == SUPPRESSED_NO_CHANNEL
        assert row["delivery_method"] is None

    def test_a_broken_tray_counts_as_no_channel(
        self,
        database: Database,
        settings: SettingsService,
        activity: ActivityRepository,
        no_toasts: None,
    ) -> None:
        notifier = Notifier(settings, activity, tray=FakeTray(fail=True))

        assert notifier.notify(notifier_module.back_in_stock("Kettle", 1)) is False
        assert last_row(database)["suppressed_why"] == SUPPRESSED_NO_CHANNEL

    def test_every_outcome_is_written_down(
        self,
        database: Database,
        settings: SettingsService,
        activity: ActivityRepository,
        toasts: FakeToastModule,
    ) -> None:
        settings.set_notification(NotificationKind.BACK_IN_STOCK, False)
        notifier = Notifier(settings, activity)

        notifier.notify(notifier_module.back_in_stock("Kettle", 1))
        notifier.notify(notifier_module.login_expired())
        notifier.notify(notifier_module.login_expired())

        assert row_count(database) == 3


class TestAvailableChannel:
    def test_windows_notifications_when_toasts_work(
        self,
        settings: SettingsService,
        activity: ActivityRepository,
        toasts: FakeToastModule,
    ) -> None:
        notifier = Notifier(settings, activity, tray=FakeTray())
        assert notifier.available_channel() == CHANNEL_TOAST

    def test_tray_only_when_toasts_cannot_be_sent(
        self,
        settings: SettingsService,
        activity: ActivityRepository,
        no_toasts: None,
    ) -> None:
        notifier = Notifier(settings, activity, tray=FakeTray())
        assert notifier.available_channel() == CHANNEL_TRAY

    def test_none_when_there_is_nothing_at_all(
        self,
        settings: SettingsService,
        activity: ActivityRepository,
        no_toasts: None,
    ) -> None:
        notifier = Notifier(settings, activity, tray=None)
        assert notifier.available_channel() == CHANNEL_NONE

    def test_a_failed_toast_downgrades_the_reported_channel(
        self,
        settings: SettingsService,
        activity: ActivityRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        module = FakeToastModule(fail_on_show=True)
        monkeypatch.setattr(notifier_module, "_load_windows_toasts", lambda: module)
        notifier = Notifier(settings, activity, tray=FakeTray())
        assert notifier.available_channel() == CHANNEL_TOAST

        notifier.notify(notifier_module.back_in_stock("Kettle", 1))

        assert notifier.available_channel() == CHANNEL_TRAY


class TestSelfTest:
    def test_it_reports_success_and_explains_what_to_check(
        self,
        settings: SettingsService,
        activity: ActivityRepository,
        toasts: FakeToastModule,
    ) -> None:
        notifier = Notifier(settings, activity)

        worked, explanation = notifier.self_test()

        assert worked is True
        assert len(toasts.shown) == 1
        assert "Windows Settings" in explanation

    def test_it_is_honest_when_only_the_tray_is_available(
        self,
        settings: SettingsService,
        activity: ActivityRepository,
        no_toasts: None,
    ) -> None:
        tray = FakeTray()
        notifier = Notifier(settings, activity, tray=tray)

        worked, explanation = notifier.self_test()

        assert worked is False
        assert len(tray.messages) == 1
        assert "clock" in explanation

    def test_it_reports_failure_with_no_channel(
        self,
        database: Database,
        settings: SettingsService,
        activity: ActivityRepository,
        no_toasts: None,
    ) -> None:
        notifier = Notifier(settings, activity, tray=None)

        worked, explanation = notifier.self_test()

        assert worked is False
        assert explanation
        assert last_row(database)["suppressed_why"] == SUPPRESSED_NO_CHANNEL

    def test_the_test_notification_ignores_the_switches(
        self,
        settings: SettingsService,
        activity: ActivityRepository,
        toasts: FakeToastModule,
    ) -> None:
        """A user asking "does this work?" gets an answer either way."""
        settings.update(notifications_enabled=False)
        notifier = Notifier(settings, activity)

        assert notifier.self_test()[0] is True


class TestActionsReachTheGuiThread:
    def test_the_slot_re_emits_the_payload(
        self, settings: SettingsService, activity: ActivityRepository
    ) -> None:
        notifier = Notifier(settings, activity)
        seen: list[str] = []
        notifier.action_invoked.connect(seen.append)

        # The queued call from the WinRT thread lands on this slot.
        notifier._emit_action("purchase/7")

        assert seen == ["purchase/7"]


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------

#: A real Amazon title is long enough to overrun any sensible body limit.
LONG_PRODUCT = (
    "Sony WH-1000XM5 Wireless Industry Leading Noise Canceling Headphones "
    "with Auto Noise Canceling Optimizer, Crystal Clear Hands-Free Calling, "
    "Black"
)

ORDER_NUMBER = "114-7702148-3963028"

#: Values that must never reach a notification. A toast is readable on the
#: lock screen and stays in the Action Center.
FORBIDDEN_IN_BODY = (
    ORDER_NUMBER,
    "Visa",
    "Mastercard",
    "American Express",
    "ending in",
    "4242",
    "Evergreen Terrace",
    "Springfield",
)

ORDER_NUMBER_SHAPE = re.compile(r"\d{3}-\d{7}-\d{7}")


def all_requests() -> list[tuple[NotificationKind, NotificationRequest]]:
    """One request per builder, with values a real caller would pass."""
    return [
        (
            NotificationKind.TARGET_PRICE_REACHED,
            notifier_module.target_price_reached(
                LONG_PRODUCT, Money(8999), Money(9000), 11
            ),
        ),
        (
            NotificationKind.BACK_IN_STOCK,
            notifier_module.back_in_stock(LONG_PRODUCT, 11),
        ),
        (
            NotificationKind.SELLER_CHANGED,
            notifier_module.seller_changed(
                LONG_PRODUCT, "Amazon.com", "Definitely Not A Reseller Limited", 11
            ),
        ),
        (
            NotificationKind.PURCHASE_READY,
            notifier_module.purchase_ready(LONG_PRODUCT, Money(12_345), 7),
        ),
        (
            NotificationKind.PURCHASE_COMPLETED,
            notifier_module.purchase_completed(
                LONG_PRODUCT, Money(12_345), ORDER_NUMBER, 7
            ),
        ),
        (
            NotificationKind.PURCHASE_BLOCKED,
            notifier_module.purchase_blocked(
                LONG_PRODUCT, "The total was higher than the limit you set.", 7
            ),
        ),
        (NotificationKind.LOGIN_EXPIRED, notifier_module.login_expired()),
        (NotificationKind.VERIFICATION_NEEDED, notifier_module.verification_needed()),
        (
            NotificationKind.MONITORING_ERROR,
            notifier_module.monitoring_error(
                LONG_PRODUCT, "Amazon did not answer in time."
            ),
        ),
    ]


BUILT = all_requests()


class TestCopy:
    def test_there_is_a_builder_for_every_kind(self) -> None:
        assert {kind for kind, _ in BUILT} == set(NotificationKind)

    @pytest.mark.parametrize(
        ("kind", "built"), BUILT, ids=[kind.value for kind, _ in BUILT]
    )
    def test_the_kind_matches_the_builder(
        self, kind: NotificationKind, built: NotificationRequest
    ) -> None:
        assert built.kind is kind

    @pytest.mark.parametrize(
        ("kind", "built"), BUILT, ids=[kind.value for kind, _ in BUILT]
    )
    def test_it_fits_on_screen(
        self, kind: NotificationKind, built: NotificationRequest
    ) -> None:
        assert 0 < len(built.title) <= MAX_TITLE_CHARS
        assert 0 < len(built.body) <= MAX_BODY_CHARS

    @pytest.mark.parametrize(
        ("kind", "built"), BUILT, ids=[kind.value for kind, _ in BUILT]
    )
    def test_it_keeps_sensitive_purchase_detail_off_the_lock_screen(
        self, kind: NotificationKind, built: NotificationRequest
    ) -> None:
        for forbidden in FORBIDDEN_IN_BODY:
            assert forbidden not in built.body
            assert forbidden not in built.title
        assert ORDER_NUMBER_SHAPE.search(built.body) is None

    @pytest.mark.parametrize(
        ("kind", "built"), BUILT, ids=[kind.value for kind, _ in BUILT]
    )
    def test_it_reads_as_plain_english(
        self, kind: NotificationKind, built: NotificationRequest
    ) -> None:
        assert "_" not in built.title
        assert "_" not in built.body
        assert kind.value not in built.body

    @pytest.mark.parametrize(
        ("kind", "built"), BUILT, ids=[kind.value for kind, _ in BUILT]
    )
    def test_the_dedupe_key_names_the_kind(
        self, kind: NotificationKind, built: NotificationRequest
    ) -> None:
        assert built.dedupe_key.startswith(kind.value)

    def test_a_completed_purchase_says_an_order_was_placed(self) -> None:
        request = notifier_module.purchase_completed(
            "Kettle", Money(1999), ORDER_NUMBER, 7
        )
        assert request.title == "Order placed"
        assert "$19.99" in request.body

    def test_prices_are_shown(self) -> None:
        request = notifier_module.target_price_reached(
            "Kettle", Money(8999), Money(9000), 11
        )
        assert "$89.99" in request.body
        assert "$90.00" in request.body

    def test_the_product_is_still_identifiable_after_trimming(self) -> None:
        request = notifier_module.back_in_stock(LONG_PRODUCT, 11)
        assert request.body.startswith("Sony WH-1000XM5")
