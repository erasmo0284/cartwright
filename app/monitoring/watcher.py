"""Deciding what one watch check means.

Pure logic: given the rules, the previous observation and the new one, work
out the watch's new status, what to tell the user, and -- for an automatic
watch -- whether the conditions to buy are met.

Kept free of I/O so every combination can be tested directly, and kept free
of its own rule checks so that the decision to buy comes from the same
:class:`~app.purchasing.purchase_guard.PurchaseGuard` the purchase itself
uses. A watch cannot authorise a purchase the guard would refuse.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import NotificationKind
from app.core.money import Money
from app.database.records import PriceObservation, WatchAction, WatchStatus
from app.purchasing.models import ProductSnapshot, PurchaseRules, normalise_label
from app.purchasing.purchase_guard import GUARD
from app.purchasing.validation import GuardReport


@dataclass(frozen=True)
class CheckDecision:
    """What a completed check means."""

    status: WatchStatus
    summary: str
    report: GuardReport
    target_reached: bool = False
    back_in_stock: bool = False
    seller_changed: bool = False
    price_dropped: bool = False
    should_buy: bool = False
    notifications: tuple[NotificationKind, ...] = field(default_factory=tuple)

    @property
    def rules_satisfied(self) -> bool:
        return self.report.passed


def _target_met(price: Money | None, target: Money | None) -> bool:
    """Whether ``price`` is at or below ``target``.

    A missing price never satisfies a target: an unreadable price must not be
    treated as a bargain.
    """
    if target is None:
        return True
    if price is None:
        return False
    if price.currency != target.currency:
        return False
    return price <= target


def evaluate_check(
    *,
    rules: PurchaseRules,
    snapshot: ProductSnapshot,
    previous: PriceObservation | None,
    action: WatchAction,
    trigger_in_stock: bool,
    trigger_target_price: bool,
) -> CheckDecision:
    """Turn a fresh observation into a decision.

    ``should_buy`` is true only when every one of these holds:

    * the watch is set to buy automatically;
    * each enabled trigger is satisfied;
    * the Purchase Guard's product-level checks all pass.

    The guard runs again, in full, before anything is ordered. This is the
    first of the two gates, not a substitute for it.
    """
    report = GUARD.check_product(rules, snapshot)
    target = rules.max_item_price
    price = snapshot.price

    in_stock = snapshot.in_stock
    was_out_of_stock = previous is not None and previous.in_stock is False
    back_in_stock = in_stock and was_out_of_stock

    target_reached = in_stock and _target_met(price, target)

    price_dropped = bool(
        price is not None
        and previous is not None
        and previous.price is not None
        and previous.price.currency == price.currency
        and price < previous.price
    )

    seller_changed = _seller_changed(rules, snapshot)

    # Status reflects what the user is waiting for, not merely what happened.
    if not in_stock:
        status = WatchStatus.OUT_OF_STOCK
    elif target is None:
        status = WatchStatus.WATCHING
    elif target_reached:
        status = WatchStatus.TARGET_REACHED
    else:
        status = WatchStatus.WAITING_FOR_PRICE

    triggers_met = True
    if trigger_in_stock and not in_stock:
        triggers_met = False
    if trigger_target_price and not _target_met(price, target):
        triggers_met = False

    should_buy = (
        action is WatchAction.BUY and triggers_met and report.passed and in_stock
    )

    notifications: list[NotificationKind] = []
    if seller_changed:
        notifications.append(NotificationKind.SELLER_CHANGED)
    if back_in_stock:
        notifications.append(NotificationKind.BACK_IN_STOCK)
    if target_reached and target is not None and action is WatchAction.NOTIFY:
        notifications.append(NotificationKind.TARGET_PRICE_REACHED)

    return CheckDecision(
        status=status,
        summary=_summarise(snapshot, target, status),
        report=report,
        target_reached=target_reached,
        back_in_stock=back_in_stock,
        seller_changed=seller_changed,
        price_dropped=price_dropped,
        should_buy=should_buy,
        notifications=tuple(notifications),
    )


def _seller_changed(rules: PurchaseRules, snapshot: ProductSnapshot) -> bool:
    """Whether the seller differs from the one recorded at setup."""
    if not rules.expected_seller:
        return False
    expected = normalise_label(rules.expected_seller)
    actual = normalise_label(snapshot.seller)
    if not actual:
        # The seller could not be read. That is worth telling the user about,
        # because it will block a purchase.
        return True
    return expected != actual


def _summarise(
    snapshot: ProductSnapshot, target: Money | None, status: WatchStatus
) -> str:
    """A short line for the watch list and the activity feed."""
    if not snapshot.in_stock:
        return snapshot.availability_text or "Out of stock"

    price_text = snapshot.price.format() if snapshot.price else "price not shown"
    if status is WatchStatus.TARGET_REACHED and target is not None:
        return f"{price_text} - at or below your target of {target.format()}"
    if target is not None:
        return f"{price_text} - waiting for {target.format()}"
    return f"In stock at {price_text}"
