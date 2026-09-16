"""Purchase rule sets.

Rule sets are never shared between jobs. Each watch job and each purchase job
owns its own row, so editing one job's limits can never silently change
another's -- which would otherwise be a way for a purchase to happen under
rules the user did not intend.
"""

from __future__ import annotations

import json
import logging

from app.core.timeutil import now_iso
from app.database.database import Database
from app.database.records import rules_from_row
from app.purchasing.models import PurchaseRules

logger = logging.getLogger("app.database.rules")


class RulesRepository:
    """Stores and retrieves :class:`PurchaseRules`."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def get(self, rules_id: int) -> PurchaseRules | None:
        row = self._db.query_one(
            "SELECT * FROM purchase_rules WHERE id = ?", (rules_id,)
        )
        return rules_from_row(row) if row else None

    def create(self, rules: PurchaseRules) -> PurchaseRules:
        """Insert a new rule set and return it with its assigned id."""
        stamp = now_iso()
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO purchase_rules (
                    currency, quantity, max_item_price_cents, max_order_total_cents,
                    seller_policy, approved_sellers_json, condition_policy,
                    expected_asin, expected_variation_json, expected_seller,
                    expected_ships_from, expected_address_label,
                    expected_payment_label, require_address_match,
                    require_payment_match, allow_addons, allow_subscription,
                    require_prime, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._to_params(rules) + (stamp, stamp),
            )
            rules_id = int(cursor.lastrowid)
        stored = self.get(rules_id)
        if stored is None:  # pragma: no cover - insert guarantees a row
            raise RuntimeError("Rule set missing immediately after insert")
        return stored

    def update(self, rules_id: int, rules: PurchaseRules) -> PurchaseRules:
        """Replace every field of an existing rule set."""
        with self._db.transaction() as conn:
            conn.execute(
                """
                UPDATE purchase_rules SET
                    currency = ?, quantity = ?, max_item_price_cents = ?,
                    max_order_total_cents = ?, seller_policy = ?,
                    approved_sellers_json = ?, condition_policy = ?,
                    expected_asin = ?, expected_variation_json = ?,
                    expected_seller = ?, expected_ships_from = ?,
                    expected_address_label = ?, expected_payment_label = ?,
                    require_address_match = ?, require_payment_match = ?,
                    allow_addons = ?, allow_subscription = ?, require_prime = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                self._to_params(rules) + (now_iso(), rules_id),
            )
        stored = self.get(rules_id)
        if stored is None:
            raise KeyError(f"No rule set with id {rules_id}")
        return stored

    def duplicate(self, rules_id: int) -> PurchaseRules:
        """Copy a rule set, so a new job starts from an existing one safely."""
        existing = self.get(rules_id)
        if existing is None:
            raise KeyError(f"No rule set with id {rules_id}")
        return self.create(existing)

    @staticmethod
    def _to_params(rules: PurchaseRules) -> tuple[object, ...]:
        return (
            rules.currency,
            rules.quantity,
            rules.max_item_price.cents if rules.max_item_price else None,
            rules.max_order_total.cents if rules.max_order_total else None,
            rules.seller_policy.value,
            json.dumps(list(rules.approved_sellers)),
            rules.condition_policy.value,
            rules.expected_asin.strip().upper(),
            rules.expected_variation.to_json()
            if not rules.expected_variation.is_empty
            else None,
            rules.expected_seller,
            rules.expected_ships_from,
            rules.expected_address_label,
            rules.expected_payment_label,
            int(rules.require_address_match),
            int(rules.require_payment_match),
            int(rules.allow_addons),
            int(rules.allow_subscription),
            int(rules.require_prime),
        )
