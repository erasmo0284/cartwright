"""Tests for :mod:`app.config`."""

from __future__ import annotations

import json

import pytest

from app.config import (
    AppSettings,
    CloseButtonAction,
    NotificationKind,
    SettingsService,
    ThemePreference,
)
from app.database.database import Database
from app.purchasing.models import ConditionPolicy, PurchaseMode, SellerPolicy


@pytest.fixture
def settings(database: Database) -> SettingsService:
    return SettingsService(database)


class TestDefaults:
    def test_test_mode_is_on_for_a_new_install(self, settings: SettingsService) -> None:
        """A fresh copy must not be able to place an order."""
        assert settings.current.test_mode is True

    def test_auto_buy_is_not_acknowledged_initially(
        self, settings: SettingsService
    ) -> None:
        assert settings.current.auto_buy_acknowledged is False

    def test_conservative_purchase_defaults(self, settings: SettingsService) -> None:
        current = settings.current
        assert current.default_purchase_mode is PurchaseMode.ASSISTED
        assert current.default_condition_policy is ConditionPolicy.NEW_ONLY
        assert current.default_seller_policy is SellerPolicy.AMAZON_ONLY
        assert current.default_quantity == 1

    def test_default_interval_respects_the_floor(
        self, settings: SettingsService
    ) -> None:
        from app.config import MIN_CHECK_INTERVAL_SECONDS

        assert settings.current.default_check_interval_seconds >= MIN_CHECK_INTERVAL_SECONDS

    def test_not_connected_initially(self, settings: SettingsService) -> None:
        assert settings.current.amazon_connected is False


class TestPersistence:
    def test_update_round_trips(self, database: Database) -> None:
        service = SettingsService(database)
        service.update(test_mode=False, default_quantity=3)

        reread = SettingsService(database)
        assert reread.current.test_mode is False
        assert reread.current.default_quantity == 3

    def test_enum_round_trips(self, database: Database) -> None:
        service = SettingsService(database)
        service.update(
            default_purchase_mode=PurchaseMode.AUTOMATIC,
            close_button_action=CloseButtonAction.EXIT,
            theme=ThemePreference.DARK,
        )
        reread = SettingsService(database).current
        assert reread.default_purchase_mode is PurchaseMode.AUTOMATIC
        assert reread.close_button_action is CloseButtonAction.EXIT
        assert reread.theme is ThemePreference.DARK

    def test_unknown_setting_name_raises(self, settings: SettingsService) -> None:
        with pytest.raises(KeyError, match="Unknown setting"):
            settings.update(definitely_not_a_setting=True)

    def test_change_signal_carries_the_new_snapshot(
        self, settings: SettingsService
    ) -> None:
        received: list[AppSettings] = []
        settings.changed.connect(received.append)
        settings.update(default_quantity=7)
        assert len(received) == 1
        assert received[0].default_quantity == 7

    def test_empty_update_is_a_no_op(self, settings: SettingsService) -> None:
        received: list[AppSettings] = []
        settings.changed.connect(received.append)
        settings.update()
        assert not received

    def test_snapshots_are_immutable(self, settings: SettingsService) -> None:
        snapshot = settings.current
        settings.update(default_quantity=5)
        assert snapshot.default_quantity == 1
        assert settings.current.default_quantity == 5


class TestCorruptedValues:
    def test_unrecognised_enum_value_falls_back_to_the_default(
        self, database: Database
    ) -> None:
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES "
                "('default_seller_policy', ?, '2026-01-01T00:00:00.000000Z')",
                (json.dumps("not_a_policy"),),
            )
        assert SettingsService(database).current.default_seller_policy is (
            SellerPolicy.AMAZON_ONLY
        )

    def test_unreadable_json_is_ignored(self, database: Database) -> None:
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES "
                "('default_quantity', 'not json', '2026-01-01T00:00:00.000000Z')"
            )
        assert SettingsService(database).current.default_quantity == 1

    def test_wrong_type_falls_back(self, database: Database) -> None:
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES "
                "('default_quantity', ?, '2026-01-01T00:00:00.000000Z')",
                (json.dumps("many"),),
            )
        assert SettingsService(database).current.default_quantity == 1

    def test_settings_from_a_newer_version_are_preserved(
        self, database: Database
    ) -> None:
        """A downgrade must not destroy a newer version's settings."""
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES "
                "('future_feature', ?, '2026-01-01T00:00:00.000000Z')",
                (json.dumps(True),),
            )
        service = SettingsService(database)
        service.update(default_quantity=2)
        remaining = database.query_scalar(
            "SELECT value FROM settings WHERE key = 'future_feature'"
        )
        assert remaining is not None


class TestNotifications:
    def test_all_enabled_by_default(self, settings: SettingsService) -> None:
        for kind in NotificationKind:
            assert settings.current.notification_allowed(kind), kind

    def test_toggling_one_off(self, settings: SettingsService) -> None:
        settings.set_notification(NotificationKind.MONITORING_ERROR, False)
        current = settings.current
        assert not current.notification_allowed(NotificationKind.MONITORING_ERROR)
        assert current.notification_allowed(NotificationKind.BACK_IN_STOCK)

    def test_purchase_critical_notifications_cannot_be_disabled(
        self, settings: SettingsService
    ) -> None:
        """The user must always learn about money being spent."""
        for kind in (
            NotificationKind.PURCHASE_COMPLETED,
            NotificationKind.PURCHASE_BLOCKED,
            NotificationKind.PURCHASE_READY,
        ):
            with pytest.raises(ValueError):
                settings.set_notification(kind, False)

    def test_master_switch_does_not_silence_purchase_notifications(
        self, settings: SettingsService
    ) -> None:
        settings.update(notifications_enabled=False)
        current = settings.current
        assert current.notification_allowed(NotificationKind.PURCHASE_COMPLETED)
        assert current.notification_allowed(NotificationKind.PURCHASE_BLOCKED)
        assert not current.notification_allowed(NotificationKind.BACK_IN_STOCK)

    def test_toggles_round_trip(self, database: Database) -> None:
        service = SettingsService(database)
        service.set_notification(NotificationKind.SELLER_CHANGED, False)
        reread = SettingsService(database).current
        assert not reread.notification_allowed(NotificationKind.SELLER_CHANGED)

    def test_every_kind_has_a_label(self) -> None:
        for kind in NotificationKind:
            assert kind.label and "_" not in kind.label


class TestReset:
    def test_reset_restores_defaults_but_keeps_the_account(
        self, settings: SettingsService
    ) -> None:
        settings.update(
            test_mode=False,
            default_quantity=9,
            amazon_connected=True,
            amazon_account_label="John",
        )
        after = settings.reset_to_defaults()
        assert after.test_mode is True
        assert after.default_quantity == 1
        assert after.amazon_connected is True
        assert after.amazon_account_label == "John"
