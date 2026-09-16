"""Tests for schema migration and the database-level purchase-safety rules.

The three partial unique indexes tested here are the last line of defence
against a duplicate order: even if every service-layer check were bypassed,
SQLite itself refuses the write.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from app.core.timeutil import now_iso
from app.database.database import Database, restore_backup
from app.database.migrations import MIGRATIONS
from app.paths import AppPaths


def _insert_product(db: Database, asin: str = "B07XYZ1234") -> int:
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO products (asin, marketplace, title, created_at, updated_at) "
            "VALUES (?, 'www.amazon.com', 'Test product', ?, ?)",
            (asin, now_iso(), now_iso()),
        )
    return int(cursor.lastrowid)


def _insert_rules(db: Database, asin: str = "B07XYZ1234") -> int:
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO purchase_rules (expected_asin, created_at, updated_at) "
            "VALUES (?, ?, ?)",
            (asin, now_iso(), now_iso()),
        )
    return int(cursor.lastrowid)


def _insert_purchase_job(
    db: Database,
    product_id: int,
    rules_id: int,
    *,
    state: str = "created",
    key: str = "key-1",
) -> int:
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO purchase_jobs
                (product_id, rules_id, mode, state, idempotency_key,
                 created_at, updated_at, state_changed_at)
            VALUES (?, ?, 'assisted', ?, ?, ?, ?, ?)
            """,
            (product_id, rules_id, state, key, now_iso(), now_iso(), now_iso()),
        )
    return int(cursor.lastrowid)


class TestMigration:
    def test_fresh_database_reaches_target_version(self, database: Database) -> None:
        assert database.current_version() == database.target_version()
        assert database.target_version() == len(MIGRATIONS)

    def test_migration_is_idempotent(self, database: Database) -> None:
        before = database.current_version()
        assert database.migrate() == before
        assert database.migrate() == before

    def test_expected_tables_exist(self, database: Database) -> None:
        rows = database.query_all(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
        names = {row["name"] for row in rows}
        for expected in (
            "settings",
            "products",
            "product_variations",
            "purchase_rules",
            "watch_jobs",
            "price_history",
            "purchase_jobs",
            "purchase_state_transitions",
            "purchase_attempts",
            "guard_reports",
            "guard_checks",
            "orders",
            "activity_events",
            "notification_events",
            "automation_diagnostics",
            "schema_migrations",
        ):
            assert expected in names, f"missing table {expected}"

    def test_foreign_keys_are_enforced(self, database: Database) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            with database.transaction() as conn:
                conn.execute(
                    "INSERT INTO price_history (product_id, observed_at) VALUES (9999, ?)",
                    (now_iso(),),
                )

    def test_wal_mode_is_active(self, database: Database) -> None:
        mode = database.query_scalar("PRAGMA journal_mode")
        assert str(mode).lower() == "wal"

    def test_integrity_check_passes(self, database: Database) -> None:
        ok, detail = database.integrity_check()
        assert ok, detail

    def test_rejects_database_from_a_newer_application(
        self, database: Database
    ) -> None:
        from app.core.errors import AppError, ErrorCode

        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) "
                "VALUES (?, 'future', ?)",
                (database.target_version() + 5, now_iso()),
            )
        with pytest.raises(AppError) as excinfo:
            database.migrate()
        assert excinfo.value.code is ErrorCode.DATABASE_ERROR


class TestConstraints:
    def test_asin_is_unique_per_marketplace(self, database: Database) -> None:
        _insert_product(database, "B07XYZ1234")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_product(database, "B07XYZ1234")

    def test_quantity_bounds(self, database: Database) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            with database.transaction() as conn:
                conn.execute(
                    "INSERT INTO purchase_rules (expected_asin, quantity, "
                    "created_at, updated_at) VALUES ('B07XYZ1234', 0, ?, ?)",
                    (now_iso(), now_iso()),
                )
        with pytest.raises(sqlite3.IntegrityError):
            with database.transaction() as conn:
                conn.execute(
                    "INSERT INTO purchase_rules (expected_asin, quantity, "
                    "created_at, updated_at) VALUES ('B07XYZ1234', 99, ?, ?)",
                    (now_iso(), now_iso()),
                )

    def test_negative_price_limits_are_refused(self, database: Database) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            with database.transaction() as conn:
                conn.execute(
                    "INSERT INTO purchase_rules (expected_asin, "
                    "max_item_price_cents, created_at, updated_at) "
                    "VALUES ('B07XYZ1234', -1, ?, ?)",
                    (now_iso(), now_iso()),
                )

    def test_unknown_purchase_state_is_refused(self, database: Database) -> None:
        product_id = _insert_product(database)
        rules_id = _insert_rules(database)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_purchase_job(
                database, product_id, rules_id, state="definitely_not_a_state"
            )

    def test_watch_interval_floor(self, database: Database) -> None:
        product_id = _insert_product(database)
        rules_id = _insert_rules(database)
        with pytest.raises(sqlite3.IntegrityError):
            with database.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO watch_jobs
                        (product_id, rules_id, interval_seconds, next_check_at,
                         created_at, updated_at)
                    VALUES (?, ?, 5, ?, ?, ?)
                    """,
                    (product_id, rules_id, now_iso(), now_iso(), now_iso()),
                )


class TestDuplicatePurchaseProtection:
    """The three schema-level guarantees against a double order."""

    def test_only_one_inflight_purchase_job_per_product(
        self, database: Database
    ) -> None:
        product_id = _insert_product(database)
        rules_id = _insert_rules(database)
        _insert_purchase_job(database, product_id, rules_id, key="a")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_purchase_job(database, product_id, rules_id, key="b")

    def test_unknown_state_still_blocks_a_new_attempt(
        self, database: Database
    ) -> None:
        """An unresolved outcome must not be retried automatically."""
        product_id = _insert_product(database)
        rules_id = _insert_rules(database)
        job_id = _insert_purchase_job(database, product_id, rules_id, key="a")
        with database.transaction() as conn:
            conn.execute(
                "UPDATE purchase_jobs SET state = 'unknown' WHERE id = ?", (job_id,)
            )

        with pytest.raises(sqlite3.IntegrityError):
            _insert_purchase_job(database, product_id, rules_id, key="b")

    def test_terminal_state_frees_the_product(self, database: Database) -> None:
        product_id = _insert_product(database)
        rules_id = _insert_rules(database)
        job_id = _insert_purchase_job(database, product_id, rules_id, key="a")
        for terminal in ("confirmed", "blocked", "failed", "cancelled"):
            with database.transaction() as conn:
                conn.execute(
                    "UPDATE purchase_jobs SET state = ? WHERE id = ?",
                    (terminal, job_id),
                )
            second = _insert_purchase_job(
                database, product_id, rules_id, key=f"k-{terminal}"
            )
            with database.transaction() as conn:
                conn.execute(
                    "DELETE FROM purchase_jobs WHERE id = ?", (second,)
                )

    def test_idempotency_key_is_unique(self, database: Database) -> None:
        product_a = _insert_product(database, "B000000001")
        product_b = _insert_product(database, "B000000002")
        rules_id = _insert_rules(database)
        _insert_purchase_job(database, product_a, rules_id, key="same")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_purchase_job(database, product_b, rules_id, key="same")

    def test_at_most_one_submitted_attempt_per_job(self, database: Database) -> None:
        product_id = _insert_product(database)
        rules_id = _insert_rules(database)
        job_id = _insert_purchase_job(database, product_id, rules_id)

        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO purchase_attempts (purchase_job_id, attempt_number, "
                "submit_token, submitted, started_at) VALUES (?, 1, 'tok-1', 1, ?)",
                (job_id, now_iso()),
            )

        with pytest.raises(sqlite3.IntegrityError):
            with database.transaction() as conn:
                conn.execute(
                    "INSERT INTO purchase_attempts (purchase_job_id, attempt_number, "
                    "submit_token, submitted, started_at) VALUES (?, 2, 'tok-2', 1, ?)",
                    (job_id, now_iso()),
                )

    def test_unsubmitted_attempts_may_repeat(self, database: Database) -> None:
        """Validation-only attempts (test mode, blocked runs) are not limited."""
        product_id = _insert_product(database)
        rules_id = _insert_rules(database)
        job_id = _insert_purchase_job(database, product_id, rules_id)
        for number in range(1, 4):
            with database.transaction() as conn:
                conn.execute(
                    "INSERT INTO purchase_attempts (purchase_job_id, attempt_number, "
                    "submit_token, submitted, started_at) VALUES (?, ?, ?, 0, ?)",
                    (job_id, number, f"tok-{number}", now_iso()),
                )
        count = database.query_scalar(
            "SELECT COUNT(*) FROM purchase_attempts WHERE purchase_job_id = ?",
            (job_id,),
        )
        assert count == 3

    def test_amazon_order_number_recorded_only_once(
        self, database: Database
    ) -> None:
        product_id = _insert_product(database)
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO orders (product_id, amazon_order_number, quantity, "
                "total_cents, confirmation_status, placed_at, created_at) "
                "VALUES (?, '112-1234567-7654321', 1, 11794, 'confirmed', ?, ?)",
                (product_id, now_iso(), now_iso()),
            )
        with pytest.raises(sqlite3.IntegrityError):
            with database.transaction() as conn:
                conn.execute(
                    "INSERT INTO orders (product_id, amazon_order_number, quantity, "
                    "total_cents, confirmation_status, placed_at, created_at) "
                    "VALUES (?, '112-1234567-7654321', 1, 11794, 'confirmed', ?, ?)",
                    (product_id, now_iso(), now_iso()),
                )

    def test_orders_without_a_number_are_not_deduplicated(
        self, database: Database
    ) -> None:
        """A user-confirmed order with no readable number is still recordable."""
        product_id = _insert_product(database)
        for _ in range(2):
            with database.transaction() as conn:
                conn.execute(
                    "INSERT INTO orders (product_id, amazon_order_number, quantity, "
                    "total_cents, confirmation_status, placed_at, created_at) "
                    "VALUES (?, NULL, 1, 11794, 'confirmed_by_user', ?, ?)",
                    (product_id, now_iso(), now_iso()),
                )
        count = database.query_scalar("SELECT COUNT(*) FROM orders")
        assert count == 2


class TestConcurrency:
    def test_writes_from_several_threads_do_not_interleave(
        self, database: Database
    ) -> None:
        product_id = _insert_product(database)
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                for step in range(20):
                    with database.transaction() as conn:
                        conn.execute(
                            "INSERT INTO price_history "
                            "(product_id, observed_at, price_cents, currency) "
                            "VALUES (?, ?, ?, 'USD')",
                            (product_id, now_iso(), index * 100 + step),
                        )
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)
            finally:
                database.close_current_thread()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, errors
        assert database.query_scalar("SELECT COUNT(*) FROM price_history") == 80


class TestBackup:
    def test_backup_and_restore_round_trip(
        self, database: Database, app_paths: AppPaths
    ) -> None:
        _insert_product(database, "B0BACKUP01")
        backup_file = database.backup(reason="test")
        assert backup_file is not None and backup_file.exists()

        with database.transaction() as conn:
            conn.execute("DELETE FROM products")
        assert database.query_scalar("SELECT COUNT(*) FROM products") == 0

        database.close_all()
        restore_backup(backup_file, app_paths.database_file)

        restored = Database(app_paths.database_file, backups_dir=app_paths.backups_dir)
        try:
            assert restored.query_scalar("SELECT COUNT(*) FROM products") == 1
        finally:
            restored.close_all()

    def test_backup_retention_prunes_old_files(
        self, database: Database, app_paths: AppPaths
    ) -> None:
        from app.database.database import BACKUP_RETENTION

        for index in range(BACKUP_RETENTION + 3):
            database.backup(reason=f"r{index}")
        remaining = list(app_paths.backups_dir.glob("app-*.db"))
        assert len(remaining) <= BACKUP_RETENTION
