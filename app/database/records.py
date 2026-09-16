"""Persistence entities.

These are the shapes that come out of the database: a row plus the
conversions that turn stored text and integers back into domain types
(:class:`~app.core.money.Money`, enums, timestamps).

They are kept separate from :mod:`app.purchasing.models`, which holds pure
domain values with no notion of a row or an id. A repository returns records;
the guard consumes domain values; the UI renders either.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from app.core.money import Money
from app.core.timeutil import from_iso
from app.purchasing.models import (
    Availability,
    ConditionPolicy,
    ItemCondition,
    PurchaseMode,
    PurchaseRules,
    SellerPolicy,
    VariationSnapshot,
)
from app.purchasing.states import PurchaseState


def _money(cents: Any, currency: Any, default_currency: str = "USD") -> Money | None:
    """Rebuild a :class:`Money` from a cents column and a currency column."""
    if cents is None:
        return None
    try:
        return Money(int(cents), str(currency or default_currency))
    except (TypeError, ValueError):
        return None


def _json_list(text: Any) -> tuple[str, ...]:
    if not text:
        return ()
    try:
        loaded = json.loads(text)
    except (TypeError, ValueError):
        return ()
    if not isinstance(loaded, list):
        return ()
    return tuple(str(item) for item in loaded)


def _enum(enum_type: type[StrEnum], raw: Any, fallback: StrEnum) -> Any:
    try:
        return enum_type(raw)
    except (ValueError, TypeError):
        return fallback


class WatchStatus(StrEnum):
    """Lifecycle of a watch job. Values are persisted; never rename."""

    WATCHING = "watching"
    WAITING_FOR_PRICE = "waiting_for_price"
    TARGET_REACHED = "target_reached"
    OUT_OF_STOCK = "out_of_stock"
    PAUSED = "paused"
    NEEDS_LOGIN = "needs_login"
    NEEDS_VERIFICATION = "needs_verification"
    PURCHASE_COMPLETED = "purchase_completed"
    EXPIRED = "expired"
    ERROR = "error"

    @property
    def label(self) -> str:
        return {
            WatchStatus.WATCHING: "Watching",
            WatchStatus.WAITING_FOR_PRICE: "Waiting for price",
            WatchStatus.TARGET_REACHED: "Target reached",
            WatchStatus.OUT_OF_STOCK: "Out of stock",
            WatchStatus.PAUSED: "Paused",
            WatchStatus.NEEDS_LOGIN: "Needs sign-in",
            WatchStatus.NEEDS_VERIFICATION: "Needs verification",
            WatchStatus.PURCHASE_COMPLETED: "Purchased",
            WatchStatus.EXPIRED: "Finished",
            WatchStatus.ERROR: "Error",
        }[self]

    @property
    def is_active(self) -> bool:
        """Whether the scheduler should keep checking this job."""
        return self in {
            WatchStatus.WATCHING,
            WatchStatus.WAITING_FOR_PRICE,
            WatchStatus.TARGET_REACHED,
            WatchStatus.OUT_OF_STOCK,
            WatchStatus.ERROR,
        }

    @property
    def needs_attention(self) -> bool:
        return self in {
            WatchStatus.NEEDS_LOGIN,
            WatchStatus.NEEDS_VERIFICATION,
            WatchStatus.ERROR,
        }


class WatchAction(StrEnum):
    """What happens when a watch job's conditions are met."""

    NOTIFY = "notify"
    BUY = "buy"

    @property
    def label(self) -> str:
        return {
            WatchAction.NOTIFY: "Notify me",
            WatchAction.BUY: "Buy automatically",
        }[self]


class ActivityCategory(StrEnum):
    """Filter tabs on the Activity screen."""

    PURCHASE = "purchase"
    WATCH_CHECK = "watch_check"
    WARNING = "warning"
    ERROR = "error"
    ACCOUNT = "account"
    SYSTEM = "system"

    @property
    def label(self) -> str:
        return {
            ActivityCategory.PURCHASE: "Purchases",
            ActivityCategory.WATCH_CHECK: "Watch checks",
            ActivityCategory.WARNING: "Warnings",
            ActivityCategory.ERROR: "Errors",
            ActivityCategory.ACCOUNT: "Account",
            ActivityCategory.SYSTEM: "System",
        }[self]


class ActivitySeverity(StrEnum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    BLOCKED = "blocked"
    ERROR = "error"


class ConfirmationStatus(StrEnum):
    """How an order's existence was established."""

    CONFIRMED = "confirmed"
    CONFIRMED_BY_USER = "confirmed_by_user"
    NOT_PLACED_PER_USER = "not_placed_per_user"

    @property
    def label(self) -> str:
        return {
            ConfirmationStatus.CONFIRMED: "Confirmed by Amazon",
            ConfirmationStatus.CONFIRMED_BY_USER: "Confirmed by you",
            ConfirmationStatus.NOT_PLACED_PER_USER: "You reported it was not placed",
        }[self]


@dataclass(frozen=True)
class ProductRecord:
    """A stored product identity."""

    id: int
    asin: str
    marketplace: str
    title: str | None
    brand: str | None
    image_url: str | None
    canonical_url: str | None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def display_title(self) -> str:
        return self.title or f"Item {self.asin}"

    @property
    def product_url(self) -> str:
        return self.canonical_url or f"https://{self.marketplace}/dp/{self.asin}"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ProductRecord:
        return cls(
            id=int(row["id"]),
            asin=str(row["asin"]),
            marketplace=str(row["marketplace"]),
            title=row["title"],
            brand=row["brand"],
            image_url=row["image_url"],
            canonical_url=row["canonical_url"],
            created_at=from_iso(row["created_at"]),
            updated_at=from_iso(row["updated_at"]),
        )


@dataclass(frozen=True)
class PriceObservation:
    """One recorded observation of a product's offer."""

    id: int
    product_id: int
    observed_at: datetime | None
    price: Money | None
    in_stock: bool | None
    availability: Availability
    availability_text: str | None
    seller: str | None
    ships_from: str | None
    condition: ItemCondition
    variation_fingerprint: str | None
    watch_job_id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> PriceObservation:
        in_stock = row["in_stock"]
        if in_stock is None:
            availability = Availability.UNKNOWN
        else:
            availability = (
                Availability.IN_STOCK if int(in_stock) else Availability.OUT_OF_STOCK
            )
        return cls(
            id=int(row["id"]),
            product_id=int(row["product_id"]),
            observed_at=from_iso(row["observed_at"]),
            price=_money(row["price_cents"], row["currency"]),
            in_stock=None if in_stock is None else bool(in_stock),
            availability=availability,
            availability_text=row["availability_text"],
            seller=row["seller"],
            ships_from=row["ships_from"],
            condition=_enum(
                ItemCondition, row["item_condition"], ItemCondition.UNKNOWN
            ),
            variation_fingerprint=row["variation_fingerprint"],
            watch_job_id=row["watch_job_id"],
        )


@dataclass(frozen=True)
class WatchJobRecord:
    """A stored watch job, without its joined product or rules."""

    id: int
    product_id: int
    rules_id: int
    status: WatchStatus
    action: WatchAction
    trigger_in_stock: bool
    trigger_target_price: bool
    interval_seconds: int
    expires_at: datetime | None
    next_check_at: datetime | None
    last_checked_at: datetime | None
    last_check_summary: str | None
    last_error_code: str | None
    consecutive_failures: int
    paused_reason: str | None
    checks_performed: int
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> WatchJobRecord:
        return cls(
            id=int(row["id"]),
            product_id=int(row["product_id"]),
            rules_id=int(row["rules_id"]),
            status=_enum(WatchStatus, row["status"], WatchStatus.ERROR),
            action=_enum(WatchAction, row["action"], WatchAction.NOTIFY),
            trigger_in_stock=bool(row["trigger_in_stock"]),
            trigger_target_price=bool(row["trigger_target_price"]),
            interval_seconds=int(row["interval_seconds"]),
            expires_at=from_iso(row["expires_at"]),
            next_check_at=from_iso(row["next_check_at"]),
            last_checked_at=from_iso(row["last_checked_at"]),
            last_check_summary=row["last_check_summary"],
            last_error_code=row["last_error_code"],
            consecutive_failures=int(row["consecutive_failures"]),
            paused_reason=row["paused_reason"],
            checks_performed=int(row["checks_performed"]),
            created_at=from_iso(row["created_at"]),
            updated_at=from_iso(row["updated_at"]),
        )


@dataclass(frozen=True)
class WatchJobView:
    """A watch job with everything the watch list needs to draw a row."""

    job: WatchJobRecord
    product: ProductRecord
    rules: PurchaseRules
    latest: PriceObservation | None = None

    @property
    def current_price(self) -> Money | None:
        return self.latest.price if self.latest else None

    @property
    def target_price(self) -> Money | None:
        return self.rules.max_item_price

    @property
    def target_reached(self) -> bool:
        price, target = self.current_price, self.target_price
        if price is None or target is None:
            return False
        return price.currency == target.currency and price <= target


@dataclass(frozen=True)
class PurchaseJobRecord:
    """A stored purchase job and its state-machine position."""

    id: int
    product_id: int
    rules_id: int
    watch_job_id: int | None
    mode: PurchaseMode
    test_mode: bool
    state: PurchaseState
    idempotency_key: str
    cart_strategy: str | None
    isolation_journal: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    state_changed_at: datetime | None = None
    terminal_at: datetime | None = None
    outcome_code: str | None = None
    outcome_detail: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> PurchaseJobRecord:
        journal: Mapping[str, Any] = {}
        raw_journal = row["isolation_journal_json"]
        if raw_journal:
            try:
                loaded = json.loads(raw_journal)
                if isinstance(loaded, dict):
                    journal = loaded
            except (TypeError, ValueError):
                journal = {}
        return cls(
            id=int(row["id"]),
            product_id=int(row["product_id"]),
            rules_id=int(row["rules_id"]),
            watch_job_id=row["watch_job_id"],
            mode=_enum(PurchaseMode, row["mode"], PurchaseMode.ASSISTED),
            test_mode=bool(row["test_mode"]),
            state=_enum(PurchaseState, row["state"], PurchaseState.UNKNOWN),
            idempotency_key=str(row["idempotency_key"]),
            cart_strategy=row["cart_strategy"],
            isolation_journal=journal,
            created_at=from_iso(row["created_at"]),
            updated_at=from_iso(row["updated_at"]),
            state_changed_at=from_iso(row["state_changed_at"]),
            terminal_at=from_iso(row["terminal_at"]),
            outcome_code=row["outcome_code"],
            outcome_detail=row["outcome_detail"],
        )


@dataclass(frozen=True)
class OrderRecord:
    """A confirmed (or user-resolved) order."""

    id: int
    purchase_job_id: int | None
    product_id: int | None
    amazon_order_number: str | None
    quantity: int
    item_price: Money | None
    shipping: Money | None
    tax: Money | None
    total: Money
    seller: str | None
    condition: ItemCondition
    shipping_label: str | None
    payment_label: str | None
    delivery_estimate: str | None
    confirmation_status: ConfirmationStatus
    placed_at: datetime | None
    verified_at: datetime | None

    @property
    def order_url(self) -> str | None:
        if not self.amazon_order_number:
            return None
        return (
            "https://www.amazon.com/gp/css/order-details?orderID="
            f"{self.amazon_order_number}"
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> OrderRecord:
        currency = row["currency"]
        return cls(
            id=int(row["id"]),
            purchase_job_id=row["purchase_job_id"],
            product_id=row["product_id"],
            amazon_order_number=row["amazon_order_number"],
            quantity=int(row["quantity"]),
            item_price=_money(row["item_price_cents"], currency),
            shipping=_money(row["shipping_cents"], currency),
            tax=_money(row["tax_cents"], currency),
            total=Money(int(row["total_cents"]), str(currency or "USD")),
            seller=row["seller"],
            condition=_enum(
                ItemCondition, row["item_condition"], ItemCondition.UNKNOWN
            ),
            shipping_label=row["shipping_label"],
            payment_label=row["payment_label"],
            delivery_estimate=row["delivery_estimate"],
            confirmation_status=_enum(
                ConfirmationStatus,
                row["confirmation_status"],
                ConfirmationStatus.CONFIRMED,
            ),
            placed_at=from_iso(row["placed_at"]),
            verified_at=from_iso(row["verified_at"]),
        )


@dataclass(frozen=True)
class ActivityEvent:
    """One entry in the activity feed."""

    id: int
    created_at: datetime | None
    category: ActivityCategory
    severity: ActivitySeverity
    title: str
    detail: str | None
    product_id: int | None
    watch_job_id: int | None
    purchase_job_id: int | None
    order_id: int | None
    error_code: str | None
    amount: Money | None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ActivityEvent:
        metadata: Mapping[str, Any] = {}
        raw = row["metadata_json"]
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    metadata = loaded
            except (TypeError, ValueError):
                metadata = {}
        return cls(
            id=int(row["id"]),
            created_at=from_iso(row["created_at"]),
            category=_enum(ActivityCategory, row["category"], ActivityCategory.SYSTEM),
            severity=_enum(ActivitySeverity, row["severity"], ActivitySeverity.INFO),
            title=str(row["title"]),
            detail=row["detail"],
            product_id=row["product_id"],
            watch_job_id=row["watch_job_id"],
            purchase_job_id=row["purchase_job_id"],
            order_id=row["order_id"],
            error_code=row["error_code"],
            amount=_money(row["amount_cents"], row["currency"]),
            metadata=metadata,
        )


def rules_from_row(row: sqlite3.Row) -> PurchaseRules:
    """Rebuild a :class:`PurchaseRules` from a ``purchase_rules`` row."""
    currency = str(row["currency"] or "USD")
    return PurchaseRules(
        expected_asin=str(row["expected_asin"]),
        quantity=int(row["quantity"]),
        currency=currency,
        max_item_price=_money(row["max_item_price_cents"], currency),
        max_order_total=_money(row["max_order_total_cents"], currency),
        seller_policy=_enum(
            SellerPolicy, row["seller_policy"], SellerPolicy.AMAZON_ONLY
        ),
        approved_sellers=_json_list(row["approved_sellers_json"]),
        condition_policy=_enum(
            ConditionPolicy, row["condition_policy"], ConditionPolicy.NEW_ONLY
        ),
        expected_variation=VariationSnapshot.from_json(row["expected_variation_json"]),
        expected_seller=row["expected_seller"],
        expected_ships_from=row["expected_ships_from"],
        expected_address_label=row["expected_address_label"],
        expected_payment_label=row["expected_payment_label"],
        require_address_match=bool(row["require_address_match"]),
        require_payment_match=bool(row["require_payment_match"]),
        allow_addons=bool(row["allow_addons"]),
        allow_subscription=bool(row["allow_subscription"]),
        require_prime=bool(row["require_prime"]),
        brand=row["brand"],
        rules_id=int(row["id"]),
    )
