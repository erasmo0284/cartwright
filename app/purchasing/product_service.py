"""Product inspection: turning a pasted link into a verified product.

Separate from the purchase service because inspection is harmless -- it only
reads -- while purchasing is not. Keeping them apart means the "Check
product" button cannot accidentally share a code path with anything that
spends money.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from PySide6.QtCore import QObject, Signal

from app.automation import selectors
from app.automation.amazon_adapter import ADAPTER
from app.automation.browser_worker import BrowserSession, BrowserWorker, Priority
from app.config import SettingsService
from app.core.errors import AppError, ErrorCode
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


def suggest_rules(
    snapshot: ProductSnapshot,
    *,
    default_seller_policy: SellerPolicy,
    default_condition_policy: ConditionPolicy,
    default_quantity: int = 1,
) -> PurchaseRules:
    """Pre-fill a rule set from an observation.

    The suggested limits are deliberately equal to the observed values rather
    than padded: a limit the user did not choose should never be higher than
    the price they were shown. They can raise it themselves.
    """
    price = snapshot.price
    return PurchaseRules(
        expected_asin=snapshot.asin,
        quantity=max(1, default_quantity),
        currency=snapshot.currency,
        max_item_price=price,
        max_order_total=price,
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
