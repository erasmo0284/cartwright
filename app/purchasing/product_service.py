"""Product inspection: turning a pasted link into a verified product.

Separate from the purchase service because inspection is harmless -- it only
reads -- while purchasing is not. Keeping them apart means the "Check
product" button cannot accidentally share a code path with anything that
spends money.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Final

from PySide6.QtCore import QObject, Signal

from app.automation import selectors
from app.automation.amazon_adapter import ADAPTER
from app.automation.browser_worker import BrowserSession, BrowserWorker, Priority
from app.config import SettingsService
from app.core.errors import AppError, ErrorCode
from app.core.money import Money, parse_money
from app.database.records import (
    ActivityCategory,
    ActivitySeverity,
    ProductRecord,
)
from app.database.repositories import ActivityRepository, ProductRepository
from app.purchasing.models import (
    ConditionPolicy,
    ProductSnapshot,
    PurchaseRules,
    SellerPolicy,
    snapshot_to_metadata,
)

logger = logging.getLogger("app.purchasing.product")


@dataclass(frozen=True)
class Inspection:
    """The result of inspecting a product."""

    snapshot: ProductSnapshot
    record: ProductRecord
    #: A rule set pre-filled from what was observed, for the user to adjust.
    suggested_rules: PurchaseRules
    #: True when this product already has a live purchase or watch job.
    already_tracked: bool = False


def parse_product_reference(text: str | None) -> tuple[str, str]:
    """Turn user input into an ASIN and a marketplace.

    Accepts a full product URL, a shortened URL with the ASIN in the path, or
    a bare ASIN. Raises :class:`AppError` with a plain-English message rather
    than guessing, because checking the wrong item is worse than refusing the
    input.
    """
    asin = selectors.extract_asin(text)
    if not asin:
        raise AppError(
            ErrorCode.INVALID_URL,
            context={"input_length": len(text or "")},
        )
    marketplace = selectors.extract_marketplace(text) or selectors.DEFAULT_MARKETPLACE
    return asin, marketplace


#: How much head-room the suggested order limit leaves over the item price,
#: as a percentage. US sales tax reaches about 10%, and a delivery charge
#: under the free-shipping threshold is common; 20% covers both without
#: being so loose that the limit stops meaning anything. It is a *suggestion*
#: shown in the editor, not a rule the app applies by itself.
ORDER_TOTAL_ALLOWANCE_PERCENT: Final = 20


#: "$4.49 delivery October 14 - 29" -- a delivery charge Amazon has already
#: named on the product page. Anchored at the start and required to be
#: immediately before the word, because the same field also says "FREE
#: delivery ... on orders over $25", and reading that $25 as postage would
#: raise the suggested limit by the one number that is not a cost.
_DELIVERY_COST = re.compile(r"^\$?([\d,]+\.\d{2})\s+(?:delivery|shipping)", re.IGNORECASE)


def shipping_from_delivery_estimate(text: str | None) -> Money | None:
    """The delivery charge stated on the product page, if it names one."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.lower().startswith("free"):
        return None
    match = _DELIVERY_COST.match(cleaned)
    if not match:
        return None
    return parse_money(match.group(0))


def suggest_order_total(
    price: Money | None, quantity: int, shipping: Money | None = None
) -> Money | None:
    """A starting order-total limit: the line cost, postage, and an allowance.

    The allowance is for tax, which cannot be known before the checkout. A
    delivery charge *can* be known -- Amazon states it on the product page --
    so it is added rather than left to eat the allowance: a live rehearsal of
    a $26.99 item with $4.49 postage and $2.29 of tax came to $33.77 against a
    suggested limit of $32.39, and blocked on a limit the user never chose.

    Rounded up, so the suggestion is never a cent below what it means to
    allow. Returns ``None`` when there is no price to work from -- the guard
    then blocks for want of a limit, which is the honest outcome.
    """
    if price is None or price.cents <= 0:
        return None
    line = price.cents * max(1, quantity)
    padded = line * (100 + ORDER_TOTAL_ALLOWANCE_PERCENT)
    # Ceiling division: an allowance that rounds down is not the allowance.
    total = -(-padded // 100)
    if shipping is not None and shipping.currency == price.currency:
        total += shipping.cents
    return Money(total, price.currency)


def suggest_rules(
    snapshot: ProductSnapshot,
    *,
    default_seller_policy: SellerPolicy,
    default_condition_policy: ConditionPolicy,
    default_quantity: int = 1,
) -> PurchaseRules:
    """Pre-fill a rule set from an observation.

    The **item** limit is exactly the price that was shown: a limit the user
    did not choose should never be higher than the number in front of them.

    The **order** limit cannot work that way. An order total includes tax,
    shipping and fees that do not exist until the checkout, so suggesting the
    item price meant the first real purchase blocked on arithmetic nobody
    chose -- a live test of a $19.99 item stopped at a $21.44 total against a
    $19.99 suggested limit. The suggestion therefore carries an allowance,
    and the user sees it and can lower it.
    """
    quantity = max(1, default_quantity)
    price = snapshot.price
    return PurchaseRules(
        expected_asin=snapshot.asin,
        quantity=quantity,
        currency=snapshot.currency,
        max_item_price=price,
        max_order_total=suggest_order_total(
            price,
            quantity,
            shipping_from_delivery_estimate(snapshot.delivery_estimate),
        ),
        seller_policy=default_seller_policy,
        condition_policy=default_condition_policy,
        expected_variation=snapshot.variation,
        expected_seller=snapshot.seller,
        expected_ships_from=snapshot.ships_from,
        brand=snapshot.brand,
    )


class ProductService(QObject):
    """Inspects products and records what was observed."""

    #: An :class:`Inspection`.
    inspected = Signal(object)
    #: An :class:`AppError`.
    inspection_failed = Signal(object)
    #: task_id, step message
    progress = Signal(int, str)
    #: Emitted when an inspection was cancelled.
    inspection_cancelled = Signal()

    def __init__(
        self,
        worker: BrowserWorker,
        products: ProductRepository,
        activity: ActivityRepository,
        settings: SettingsService,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._worker = worker
        self._products = products
        self._activity = activity
        self._settings = settings
        self._inspection_tasks: set[int] = set()

        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_cancelled)

    # ---- inspection ------------------------------------------------------

    def inspect(self, text: str | None) -> int:
        """Inspect the product identified by ``text``.

        Returns the task id so the caller can cancel it. Input validation
        happens here, synchronously, so a bad link is reported instantly
        rather than after a browser round trip.
        """
        asin, marketplace = parse_product_reference(text)
        task_id = self._worker.submit(
            f"Check product {asin}",
            lambda session: self._run_inspection(session, asin, marketplace),
            priority=Priority.INTERACTIVE,
            context={"kind": "inspection", "asin": asin},
        )
        self._inspection_tasks.add(task_id)
        return task_id

    def cancel(self, task_id: int) -> None:
        self._worker.cancel(task_id)

    def _run_inspection(
        self, session: BrowserSession, asin: str, marketplace: str
    ) -> Inspection:
        snapshot = ADAPTER.inspect_product(
            session, asin=asin, marketplace=marketplace
        )
        record = self._products.upsert_from_snapshot(snapshot)
        self._products.record_observation(record.id, snapshot)
        self._products.record_variation(record.id, snapshot.variation)

        settings = self._settings.current
        rules = suggest_rules(
            snapshot,
            default_seller_policy=settings.default_seller_policy,
            default_condition_policy=settings.default_condition_policy,
            default_quantity=settings.default_quantity,
        )
        # The brand lives on the product row, and the manufacturer seller
        # policy needs it.
        if record.brand and not rules.brand:
            rules = replace(rules, brand=record.brand)

        self._activity.add(
            category=ActivityCategory.WATCH_CHECK,
            severity=ActivitySeverity.INFO,
            title=f"Checked {record.display_title}",
            detail=self._describe(snapshot),
            product_id=record.id,
            amount=snapshot.price,
            metadata=snapshot_to_metadata(snapshot),
        )
        return Inspection(
            snapshot=snapshot,
            record=record,
            suggested_rules=rules,
        )

    @staticmethod
    def _describe(snapshot: ProductSnapshot) -> str:
        parts: list[str] = []
        if snapshot.price:
            parts.append(snapshot.price.format())
        parts.append(snapshot.availability.label)
        if snapshot.seller:
            parts.append(f"sold by {snapshot.seller}")
        return ", ".join(parts)

    def refresh(self, product_id: int) -> int | None:
        """Re-check a stored product, e.g. from the watch list."""
        record = self._products.get(product_id)
        if record is None:
            return None
        return self.inspect(record.product_url)

    # ---- worker signal plumbing -----------------------------------------

    def _is_ours(self, context: object) -> bool:
        return isinstance(context, dict) and context.get("kind") == "inspection"

    def _on_progress(self, task_id: int, message: str) -> None:
        if task_id in self._inspection_tasks:
            self.progress.emit(task_id, message)

    def _on_finished(self, task_id: int, result: object, context: object) -> None:
        if not self._is_ours(context):
            return
        self._inspection_tasks.discard(task_id)
        if isinstance(result, Inspection):
            self.inspected.emit(result)

    def _on_failed(self, task_id: int, error: object, context: object) -> None:
        if not self._is_ours(context):
            return
        self._inspection_tasks.discard(task_id)
        if isinstance(error, AppError):
            asin = context.get("asin") if isinstance(context, dict) else None
            self._activity.add(
                category=ActivityCategory.ERROR,
                severity=ActivitySeverity.ERROR,
                title="Could not check the product",
                detail=error.detail,
                error_code=error.code.value,
                metadata={"asin": asin, **error.context},
            )
            self.inspection_failed.emit(error)

    def _on_cancelled(self, task_id: int, context: object) -> None:
        if not self._is_ours(context):
            return
        self._inspection_tasks.discard(task_id)
        self.inspection_cancelled.emit()
