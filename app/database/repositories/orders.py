"""Confirmed orders.

A row exists here only when Amazon's own confirmation was read, or when the
user personally resolved an uncertain outcome. Clicking the order button is
never sufficient to create one.
"""

from __future__ import annotations

import logging
import sqlite3

from app.core.errors import AppError, ErrorCode
from app.core.money import Money
from app.core.timeutil import now_iso, to_iso
from app.database.database import Database
from app.database.records import ConfirmationStatus, OrderRecord
from app.purchasing.models import (
    CheckoutSnapshot,
    ItemCondition,
    OrderConfirmation,
    PurchaseRules,
)

logger = logging.getLogger("app.database.orders")


class OrderRepository:
    """Records and retrieves orders."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def get(self, order_id: int) -> OrderRecord | None:
        row = self._db.query_one("SELECT * FROM orders WHERE id = ?", (order_id,))
        return OrderRecord.from_row(row) if row else None

    def find_by_order_number(self, order_number: str) -> OrderRecord | None:
        row = self._db.query_one(
            "SELECT * FROM orders WHERE amazon_order_number = ?", (order_number,)
        )
        return OrderRecord.from_row(row) if row else None

    def for_purchase_job(self, job_id: int) -> OrderRecord | None:
        row = self._db.query_one(
            "SELECT * FROM orders WHERE purchase_job_id = ? ORDER BY id DESC LIMIT 1",
            (job_id,),
        )
        return OrderRecord.from_row(row) if row else None

    def list_recent(self, *, limit: int = 50) -> list[OrderRecord]:
        rows = self._db.query_all(
            "SELECT * FROM orders ORDER BY placed_at DESC LIMIT ?", (limit,)
        )
        return [OrderRecord.from_row(row) for row in rows]

    def total_spent(self) -> Money | None:
        """Sum of confirmed orders, for the dashboard."""
        row = self._db.query_one(
            "SELECT SUM(total_cents) AS total, currency FROM orders "
            "WHERE confirmation_status IN ('confirmed', 'confirmed_by_user') "
            "GROUP BY currency ORDER BY total DESC LIMIT 1"
        )
        if row is None or row["total"] is None:
            return None
        return Money(int(row["total"]), str(row["currency"] or "USD"))

    def record(
        self,
        *,
        purchase_job_id: int | None,
        product_id: int | None,
        rules: PurchaseRules,
        checkout: CheckoutSnapshot,
        confirmation: OrderConfirmation,
        seller: str | None,
        condition: ItemCondition,
        status: ConfirmationStatus = ConfirmationStatus.CONFIRMED,
    ) -> OrderRecord:
        """Store a confirmed order.

        If the order number is already recorded the existing row is returned
        instead of inserting a duplicate, so a repeated confirmation read
        cannot produce two history entries for one order.
        """
        order_number = confirmation.order_number
        if order_number:
            existing = self.find_by_order_number(order_number)
            if existing is not None:
                logger.info(
                    "Order already recorded",
                    extra={"amazon_order_number": order_number},
                )
                return existing

        total = confirmation.order_total or checkout.order_total
        if total is None:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                context={"reason": "order_total_missing"},
                detail_override=(
                    "The order was placed but its total could not be recorded."
                ),
            )
        matching = checkout.lines_for(rules.expected_asin)
        quantity = sum(line.quantity for line in matching) or rules.quantity
        item_price = next(
            (line.unit_price for line in matching if line.unit_price), None
        )
        placed_at = to_iso(confirmation.observed_at)

        try:
            with self._db.transaction() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO orders (
                        purchase_job_id, product_id, amazon_order_number, quantity,
                        item_price_cents, shipping_cents, tax_cents, total_cents,
                        currency, seller, item_condition, shipping_label,
                        payment_label, delivery_estimate, confirmation_status,
                        placed_at, verified_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        purchase_job_id,
                        product_id,
                        order_number,
                        quantity,
                        item_price.cents if item_price else None,
                        checkout.shipping.cents if checkout.shipping else None,
                        checkout.tax.cents if checkout.tax else None,
                        total.cents,
                        total.currency,
                        seller,
                        condition.value,
                        checkout.address_label,
                        checkout.payment_label,
                        confirmation.delivery_estimate,
                        status.value,
                        placed_at,
                        now_iso() if confirmation.verified else None,
                        now_iso(),
                    ),
                )
                order_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            if order_number:
                existing = self.find_by_order_number(order_number)
                if existing is not None:
                    return existing
            raise AppError(
                ErrorCode.DATABASE_ERROR,
                context={"reason": "order_insert_failed"},
                cause=exc,
            ) from exc

        record = self.get(order_id)
        if record is None:  # pragma: no cover
            raise RuntimeError("Order missing immediately after insert")
        logger.info(
            "Order recorded",
            extra={
                "order_id": order_id,
                "amazon_order_number": order_number,
                "total_cents": total.cents,
                "verified": confirmation.verified,
            },
        )
        return record

    def record_user_confirmed(
        self,
        *,
        purchase_job_id: int,
        product_id: int | None,
        total: Money,
        quantity: int,
        order_number: str | None,
        note: str | None = None,
    ) -> OrderRecord:
        """Record an order the user confirmed after an uncertain outcome."""
        if order_number:
            existing = self.find_by_order_number(order_number)
            if existing is not None:
                return existing
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO orders (
                    purchase_job_id, product_id, amazon_order_number, quantity,
                    total_cents, currency, delivery_estimate,
                    confirmation_status, placed_at, verified_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    purchase_job_id,
                    product_id,
                    order_number,
                    quantity,
                    total.cents,
                    total.currency,
                    note,
                    ConfirmationStatus.CONFIRMED_BY_USER.value,
                    now_iso(),
                    now_iso(),
                    now_iso(),
                ),
            )
            order_id = int(cursor.lastrowid)
        record = self.get(order_id)
        if record is None:  # pragma: no cover
            raise RuntimeError("Order missing immediately after insert")
        return record

    def mark_verified(self, order_id: int, order_number: str | None = None) -> None:
        """Attach a later verification (for example from the orders page)."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE orders SET verified_at = ?, "
                "amazon_order_number = COALESCE(?, amazon_order_number), "
                "confirmation_status = 'confirmed' WHERE id = ?",
                (now_iso(), order_number, order_id),
            )
