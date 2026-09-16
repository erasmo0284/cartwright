"""Application settings.

Settings live in the SQLite ``settings`` table -- one JSON value per key --
so there is exactly one source of truth, it participates in the same
transactions and backups as the rest of the data, and there is no second
config file to drift out of step.

:class:`AppSettings` is an immutable snapshot. Readers hold a snapshot and
never see a half-applied change; writers go through
:meth:`SettingsService.update`, which persists and then publishes a new
snapshot.

Two defaults are deliberate and load-bearing:

* ``test_mode`` starts **on**. A freshly installed copy cannot place an order
  until the user turns test mode off themselves.
* ``auto_buy_acknowledged`` starts **off**, so automatic purchasing cannot be
  selected until the user has been shown, and accepted, what it means.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from typing import Any, Final, Mapping

from PySide6.QtCore import QObject, Signal

from app.database.database import Database
from app.purchasing.models import ConditionPolicy, PurchaseMode, SellerPolicy

logger = logging.getLogger("app.config")


class CloseButtonAction(StrEnum):
    """What the window's close button does while monitoring is active."""

    MINIMIZE_TO_TRAY = "minimize_to_tray"
    EXIT = "exit"

    @property
    def label(self) -> str:
        return {
            CloseButtonAction.MINIMIZE_TO_TRAY: "Keep running in the system tray",
            CloseButtonAction.EXIT: "Exit the application",
        }[self]


class ThemePreference(StrEnum):
    """User's appearance choice."""

    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"

    @property
    def label(self) -> str:
        return {
            ThemePreference.SYSTEM: "Match Windows",
            ThemePreference.LIGHT: "Light",
            ThemePreference.DARK: "Dark",
        }[self]


class NotificationKind(StrEnum):
    """Individually toggleable notifications."""

    TARGET_PRICE_REACHED = "target_price_reached"
    BACK_IN_STOCK = "back_in_stock"
    SELLER_CHANGED = "seller_changed"
    PURCHASE_READY = "purchase_ready"
    PURCHASE_COMPLETED = "purchase_completed"
    PURCHASE_BLOCKED = "purchase_blocked"
    LOGIN_EXPIRED = "login_expired"
    VERIFICATION_NEEDED = "verification_needed"
    MONITORING_ERROR = "monitoring_error"

    @property
    def label(self) -> str:
        return {
            NotificationKind.TARGET_PRICE_REACHED: "Target price reached",
            NotificationKind.BACK_IN_STOCK: "Product back in stock",
            NotificationKind.SELLER_CHANGED: "Seller changed",
            NotificationKind.PURCHASE_READY: "Purchase ready for confirmation",
            NotificationKind.PURCHASE_COMPLETED: "Purchase completed",
            NotificationKind.PURCHASE_BLOCKED: "Purchase blocked",
            NotificationKind.LOGIN_EXPIRED: "Amazon sign-in expired",
            NotificationKind.VERIFICATION_NEEDED: "Amazon needs verification",
            NotificationKind.MONITORING_ERROR: "Monitoring error",
        }[self]

    @property
    def always_on(self) -> bool:
        """Notifications the user cannot turn off.

        Anything that means "an order happened, or nearly did" is not
        silenceable: the user must always learn about money being spent and
        about a purchase waiting on them.
        """
        return self in {
            NotificationKind.PURCHASE_COMPLETED,
            NotificationKind.PURCHASE_BLOCKED,
            NotificationKind.PURCHASE_READY,
        }


# ---------------------------------------------------------------------------
# Monitoring intervals
# ---------------------------------------------------------------------------

#: Floor on a watch interval, matching the ``CHECK (interval_seconds >= 120)``
#: constraint in migration 1. Two minutes on a handful of products is far
#: below any rate Amazon objects to, while still preventing a user from
#: setting something abusive.
MIN_CHECK_INTERVAL_SECONDS: Final = 120

#: Offered in the interval dropdown. Labels come from
#: :func:`app.core.timeutil.format_duration`.
CHECK_INTERVAL_CHOICES: Final[tuple[int, ...]] = (
    120,
    300,
    600,
    900,
    1_800,
    3_600,
    10_800,
    21_600,
    43_200,
    86_400,
)

DEFAULT_CHECK_INTERVAL_SECONDS: Final = 300

#: Random proportion added to or subtracted from each scheduled interval, so a
#: watch list does not produce a perfectly regular request pattern.
CHECK_INTERVAL_JITTER: Final = 0.15

#: Minimum gap between any two Amazon page loads, across every watch job.
#: Adding products therefore cannot multiply the request rate.
GLOBAL_MIN_REQUEST_GAP_SECONDS: Final = 15


def _default_notifications() -> dict[str, bool]:
    return {kind.value: True for kind in NotificationKind}


@dataclass(frozen=True)
class AppSettings:
    """An immutable snapshot of every user-configurable setting."""

    # --- purchasing defaults ---------------------------------------------
    test_mode: bool = True
    default_purchase_mode: PurchaseMode = PurchaseMode.ASSISTED
    default_condition_policy: ConditionPolicy = ConditionPolicy.NEW_ONLY
    default_seller_policy: SellerPolicy = SellerPolicy.AMAZON_ONLY
    default_quantity: int = 1
    #: True once the user has read and accepted the automatic-purchase
    #: explanation. Automatic mode is unavailable until then.
    auto_buy_acknowledged: bool = False

    # --- monitoring -------------------------------------------------------
    default_check_interval_seconds: int = DEFAULT_CHECK_INTERVAL_SECONDS
    monitoring_enabled: bool = True
    continue_in_tray: bool = True
    close_button_action: CloseButtonAction = CloseButtonAction.MINIMIZE_TO_TRAY
    start_with_windows: bool = False

    # --- notifications ----------------------------------------------------
    notifications_enabled: bool = True
    notification_toggles: Mapping[str, bool] = field(
        default_factory=_default_notifications
    )

    # --- appearance -------------------------------------------------------
    theme: ThemePreference = ThemePreference.SYSTEM

    # --- diagnostics ------------------------------------------------------
    log_level: str = "INFO"
    #: Save a screenshot when an automation step fails unexpectedly. Helpful
    #: for support; the user can turn it off and delete existing captures.
    save_failure_screenshots: bool = True

    # --- account ----------------------------------------------------------
    amazon_connected: bool = False
    amazon_account_label: str | None = None
    amazon_last_verified_at: str | None = None
    marketplace: str = "www.amazon.com"

    # --- first run --------------------------------------------------------
    onboarding_completed: bool = False

    def notification_allowed(self, kind: NotificationKind) -> bool:
        """Whether ``kind`` should be delivered.

        The master switch silences everything except the purchase-critical
        kinds, which are never suppressed.
        """
        if kind.always_on:
            return True
        if not self.notifications_enabled:
            return False
        return bool(self.notification_toggles.get(kind.value, True))

    def with_notification(self, kind: NotificationKind, enabled: bool) -> AppSettings:
        toggles = dict(self.notification_toggles)
        toggles[kind.value] = enabled
        return replace(self, notification_toggles=toggles)


#: Coercion for values coming back out of JSON. Anything not listed is used
#: as-is. Keeping this table explicit means a corrupted value falls back to
#: the default instead of propagating a wrong type into the UI.
_ENUM_FIELDS: Final[Mapping[str, type[StrEnum]]] = {
    "default_purchase_mode": PurchaseMode,
    "default_condition_policy": ConditionPolicy,
    "default_seller_policy": SellerPolicy,
    "close_button_action": CloseButtonAction,
    "theme": ThemePreference,
}


class SettingsService(QObject):
    """Reads and writes settings, and publishes a snapshot on every change."""

    #: Emitted after a successful write, carrying the new snapshot.
    changed = Signal(object)

    def __init__(self, database: Database, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._database = database
        # Imported here rather than at module scope: the repositories package
        # imports the watch repository, which needs this module's interval
        # constants, so a top-level import would be circular. All SQL still
        # lives behind a repository, including this table's.
        from app.database.repositories.settings import SettingsRepository

        self._store = SettingsRepository(database)
        self._settings = AppSettings()
        self._loaded = False

    # ---- reading ---------------------------------------------------------

    @property
    def current(self) -> AppSettings:
        """The active snapshot, loading from the database on first access."""
        if not self._loaded:
            self.reload()
        return self._settings

    def reload(self) -> AppSettings:
        """Re-read every setting from the database."""
        stored = self._store.load_all()

        defaults = AppSettings()
        known = {item.name for item in fields(AppSettings)}
        values: dict[str, Any] = {}
        for name in known:
            if name not in stored:
                continue
            raw = stored[name]
            coerced = self._coerce(name, raw, getattr(defaults, name))
            if coerced is not None:
                values[name] = coerced

        unknown = set(stored) - known
        if unknown:
            # Left in place rather than deleted: a downgrade should not
            # destroy a newer version's settings.
            logger.debug("Ignoring settings from another version", extra={"keys": sorted(unknown)})

        self._settings = replace(defaults, **values)
        self._loaded = True
        return self._settings

    def _coerce(self, name: str, raw: Any, default: Any) -> Any:
        """Convert a stored JSON value to the field's type, or ``None``."""
        enum_type = _ENUM_FIELDS.get(name)
        if enum_type is not None:
            try:
                return enum_type(raw)
            except ValueError:
                logger.warning(
                    "Resetting setting with an unrecognised value",
                    extra={"key": name, "value": raw},
                )
                return None
        if isinstance(default, bool):
            return bool(raw)
        if isinstance(default, int) and not isinstance(default, bool):
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
        if name == "notification_toggles":
            if not isinstance(raw, dict):
                return None
            return {
                str(key): bool(value)
                for key, value in raw.items()
                if key in {kind.value for kind in NotificationKind}
            }
        if default is None or isinstance(default, str):
            return None if raw is None else str(raw)
        return raw

    # ---- writing ---------------------------------------------------------

    def update(self, **changes: Any) -> AppSettings:
        """Persist ``changes`` and publish the resulting snapshot.

        Unknown field names raise, so a typo in a settings screen is caught
        immediately rather than silently doing nothing.
        """
        known = {item.name for item in fields(AppSettings)}
        unexpected = set(changes) - known
        if unexpected:
            raise KeyError(f"Unknown setting(s): {sorted(unexpected)}")
        if not changes:
            return self.current

        current = self.current
        updated = replace(current, **changes)
        self._store.save({name: _to_json(value) for name, value in changes.items()})
        self._settings = updated
        logger.info("Settings updated", extra={"keys": sorted(changes)})
        self.changed.emit(updated)
        return updated

    def set_notification(self, kind: NotificationKind, enabled: bool) -> AppSettings:
        """Toggle one notification kind."""
        if kind.always_on and not enabled:
            raise ValueError(
                f"{kind.label} cannot be turned off: it reports money being spent"
            )
        toggles = dict(self.current.notification_toggles)
        toggles[kind.value] = enabled
        return self.update(notification_toggles=toggles)

    def reset_to_defaults(self) -> AppSettings:
        """Restore every setting except the stored Amazon session status.

        The account keys are preserved because clearing them would make a
        connected browser profile look disconnected.
        """
        preserved = {
            "amazon_connected",
            "amazon_account_label",
            "amazon_last_verified_at",
        }
        self._store.delete_all_except(frozenset(preserved))
        settings = self.reload()
        logger.info("Settings reset to defaults")
        self.changed.emit(settings)
        return settings


def _to_json(value: Any) -> Any:
    """Make a settings value JSON-serialisable."""
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _to_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json(item) for item in value]
    return value
