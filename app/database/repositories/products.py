"""Products, their observed variations, and price history."""

from __future__ import annotations

import logging

from app.core.timeutil import now_iso
from app.database.database import Database
from app.database.records import PriceObservation, ProductRecord
from app.purchasing.models import ProductSnapshot, VariationSnapshot

logger = logging.getLogger("app.database.products")

#: Price history rows kept per product. Enough for a useful trend line
#: without letting the database grow without bound on a long-running watch.
PRICE_HISTORY_LIMIT = 500


class ProductRepository:
    """Stores product identities and the observations made about them."""

    def __init__(self, database: Database) -> None:
        self._db = database

    # ---- products --------------------------------------------------------

    def get(self, product_id: int) -> ProductRecord | None:
        row = self._db.query_one("SELECT * FROM products WHERE id = ?", (product_id,))
        return ProductRecord.from_row(row) if row else None

    def find_by_asin(
        self, asin: str, marketplace: str = "www.amazon.com"
    ) -> ProductRecord | None:
        row = self._db.query_one(
            "SELECT * FROM products WHERE marketplace = ? AND asin = ?",
            (marketplace, asin.strip().upper()),
        )
        return ProductRecord.from_row(row) if row else None

    def upsert_from_snapshot(self, snapshot: ProductSnapshot) -> ProductRecord:
        """Create or refresh the product row for a snapshot.

        Descriptive fields are only overwritten when the snapshot actually
        has a value, so a partially parsed page cannot erase a good title or
        image that an earlier visit captured.
        """
        asin = snapshot.asin.strip().upper()
        stamp = now_iso()
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO products
                    (asin, marketplace, title, brand, image_url, canonical_url,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (marketplace, asin) DO UPDATE SET
                    title         = COALESCE(excluded.title,         products.title),
                    brand         = COALESCE(excluded.brand,         products.brand),
                    image_url     = COALESCE(excluded.image_url,     products.image_url),
                    canonical_url = COALESCE(excluded.canonical_url, products.canonical_url),
                    updated_at    = excluded.updated_at
                """,
                (
                    asin,
                    snapshot.marketplace,
                    snapshot.title,
                    snapshot.brand,
                    snapshot.image_url,
                    snapshot.url,
                    stamp,
                    stamp,
                ),
            )
        record = self.find_by_asin(asin, snapshot.marketplace)
        if record is None:  # pragma: no cover - the upsert guarantees a row
            raise RuntimeError(f"Product row missing after upsert for {asin}")
        return record

    def list_all(self) -> list[ProductRecord]:
        return [
            ProductRecord.from_row(row)
            for row in self._db.query_all(
                "SELECT * FROM products ORDER BY updated_at DESC"
            )
        ]

    def delete_orphans(self) -> int:
        """Remove products that no watch job, purchase or order references."""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                DELETE FROM products
                WHERE id NOT IN (SELECT product_id FROM watch_jobs)
                  AND id NOT IN (SELECT product_id FROM purchase_jobs)
                  AND id NOT IN (
                      SELECT product_id FROM orders WHERE product_id IS NOT NULL)
                """
            )
        return cursor.rowcount or 0

    # ---- variations ------------------------------------------------------

    def record_variation(
        self, product_id: int, variation: VariationSnapshot
    ) -> None:
        """Note that this variation was seen for this product."""
        if variation.is_empty:
            return
        stamp = now_iso()
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO product_variations
                    (product_id, fingerprint, dimensions_json,
                     first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (product_id, fingerprint) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    product_id,
                    variation.fingerprint,
                    variation.to_json(),
                    stamp,
                    stamp,
                ),
            )

    # ---- price history ---------------------------------------------------

    def record_observation(
        self,
        product_id: int,
        snapshot: ProductSnapshot,
        *,
        watch_job_id: int | None = None,
    ) -> int:
        """Append a price-history row and prune the oldest rows."""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO price_history
                    (product_id, watch_job_id, observed_at, price_cents, currency,
                     in_stock, availability_text, seller, ships_from,
                     item_condition, variation_fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    product_id,
                    watch_job_id,
                    now_iso(),
                    snapshot.price.cents if snapshot.price else None,
                    snapshot.price.currency if snapshot.price else None,
                    None
                    if snapshot.availability.value == "unknown"
                    else int(snapshot.in_stock),
                    snapshot.availability_text,
                    snapshot.seller,
                    snapshot.ships_from,
                    snapshot.condition.value,
                    snapshot.variation.fingerprint,
                ),
            )
            observation_id = int(cursor.lastrowid)
            conn.execute(
                """
                DELETE FROM price_history
                WHERE product_id = ?
                  AND id NOT IN (
                      SELECT id FROM price_history
                      WHERE product_id = ?
                      ORDER BY observed_at DESC, id DESC
                      LIMIT ?
                  )
                """,
                (product_id, product_id, PRICE_HISTORY_LIMIT),
            )
        return observation_id

    def latest_observation(self, product_id: int) -> PriceObservation | None:
        row = self._db.query_one(
            "SELECT * FROM price_history WHERE product_id = ? "
            "ORDER BY observed_at DESC, id DESC LIMIT 1",
            (product_id,),
        )
        return PriceObservation.from_row(row) if row else None

    def history(self, product_id: int, *, limit: int = 100) -> list[PriceObservation]:
        """Observations newest-first, for the product history panel."""
        rows = self._db.query_all(
            "SELECT * FROM price_history WHERE product_id = ? "
            "ORDER BY observed_at DESC, id DESC LIMIT ?",
            (product_id, limit),
        )
        return [PriceObservation.from_row(row) for row in rows]

    def price_series(
        self, product_id: int, *, limit: int = 60
    ) -> list[PriceObservation]:
        """Oldest-first observations that have a price, for the sparkline."""
        rows = self._db.query_all(
            "SELECT * FROM price_history WHERE product_id = ? "
            "AND price_cents IS NOT NULL "
            "ORDER BY observed_at DESC, id DESC LIMIT ?",
            (product_id, limit),
        )
        return [PriceObservation.from_row(row) for row in reversed(rows)]

    def lowest_price_cents(self, product_id: int) -> int | None:
        return self._db.query_scalar(
            "SELECT MIN(price_cents) FROM price_history WHERE product_id = ?",
            (product_id,),
        )
