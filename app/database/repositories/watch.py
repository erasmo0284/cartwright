"""Watch jobs and the scheduler's claim query."""

from __future__ import annotations

import logging
import random

from app.config import (
    CHECK_INTERVAL_JITTER,
    MIN_CHECK_INTERVAL_SECONDS,
)
from app.core.timeutil import iso_in, now_iso
from app.database.database import Database
from app.database.records import (
    WatchAction,
    WatchJobRecord,
    WatchJobView,
    WatchStatus,
    rules_from_row,
)
from app.database.repositories.products import ProductRepository
from app.database.records import ProductRecord

logger = logging.getLogger("app.database.watch")

#: Backoff ladder applied after consecutive failures, in seconds. Capped at an
#: hour so a watch job recovers by itself once the problem clears.
BACKOFF_LADDER: tuple[int, ...] = (30, 300, 900, 1_800, 3_600)


def jittered_delay(interval_seconds: int) -> float:
    """An interval with random jitter applied.

    Jitter keeps a watch list from producing a perfectly regular request
    pattern, which is both politer and less likely to be throttled.
    """
    interval = max(MIN_CHECK_INTERVAL_SECONDS, int(interval_seconds))
    spread = interval * CHECK_INTERVAL_JITTER
    return max(
        MIN_CHECK_INTERVAL_SECONDS,
        interval + random.uniform(-spread, spread),
    )


def backoff_delay(consecutive_failures: int) -> int:
    """Seconds to wait after ``consecutive_failures`` failed checks."""
    if consecutive_failures <= 0:
        return 0
    index = min(consecutive_failures, len(BACKOFF_LADDER)) - 1
    return BACKOFF_LADDER[index]


class WatchRepository:
    """Stores watch jobs and answers "what is due to be checked?"."""

    def __init__(self, database: Database) -> None:
        self._db = database
        self._products = ProductRepository(database)

    # ---- reading ---------------------------------------------------------

    def get(self, job_id: int) -> WatchJobRecord | None:
        row = self._db.query_one("SELECT * FROM watch_jobs WHERE id = ?", (job_id,))
        return WatchJobRecord.from_row(row) if row else None

    def view(self, job_id: int) -> WatchJobView | None:
        """A watch job joined with its product, rules and latest observation."""
        row = self._db.query_one(
            """
            SELECT w.*, p.id AS p_id, r.id AS r_id
            FROM watch_jobs w
            JOIN products       p ON p.id = w.product_id
            JOIN purchase_rules r ON r.id = w.rules_id
            WHERE w.id = ?
            """,
            (job_id,),
        )
        if row is None:
            return None
        return self._build_view(WatchJobRecord.from_row(row))

    def list_views(self, *, include_finished: bool = True) -> list[WatchJobView]:
        """Every watch job, ordered so the ones needing attention come first."""
        rows = self._db.query_all("SELECT * FROM watch_jobs ORDER BY created_at DESC")
        views = [self._build_view(WatchJobRecord.from_row(row)) for row in rows]
        if not include_finished:
            views = [
                view
                for view in views
                if view.job.status
                not in {WatchStatus.PURCHASE_COMPLETED, WatchStatus.EXPIRED}
            ]
        priority = {
            WatchStatus.NEEDS_VERIFICATION: 0,
            WatchStatus.NEEDS_LOGIN: 1,
            WatchStatus.ERROR: 2,
            WatchStatus.TARGET_REACHED: 3,
        }
        return sorted(
            views,
            key=lambda view: (
                priority.get(view.job.status, 5),
                -(view.job.id or 0),
            ),
        )

    def _build_view(self, job: WatchJobRecord) -> WatchJobView:
        product_row = self._db.query_one(
            "SELECT * FROM products WHERE id = ?", (job.product_id,)
        )
        rules_row = self._db.query_one(
            "SELECT * FROM purchase_rules WHERE id = ?", (job.rules_id,)
        )
        if product_row is None or rules_row is None:  # pragma: no cover
            raise RuntimeError(f"Watch job {job.id} has dangling references")
        product = ProductRecord.from_row(product_row)
        rules = rules_from_row(rules_row)
        # The manufacturer seller policy needs the brand, which lives on the
        # product rather than the rule set.
        if product.brand and not rules.brand:
            from dataclasses import replace

            rules = replace(rules, brand=product.brand)
        return WatchJobView(
            job=job,
            product=product,
            rules=rules,
            latest=self._products.latest_observation(job.product_id),
        )

    def counts(self) -> dict[str, int]:
        """Summary counters for the dashboard."""
        rows = self._db.query_all(
            "SELECT status, COUNT(*) AS total FROM watch_jobs GROUP BY status"
        )
        by_status = {str(row["status"]): int(row["total"]) for row in rows}
        active = sum(
            total
            for status, total in by_status.items()
            if WatchStatus(status).is_active
            if status in {item.value for item in WatchStatus}
        )
        attention = sum(
            total
            for status, total in by_status.items()
            if status in {item.value for item in WatchStatus}
            and WatchStatus(status).needs_attention
        )
        waiting = by_status.get(WatchStatus.WAITING_FOR_PRICE.value, 0)
        return {
            "watching": active,
            "needs_attention": attention,
            "waiting_for_price": waiting,
            "total": sum(by_status.values()),
        }

    # ---- writing ---------------------------------------------------------

    def create(
        self,
        *,
        product_id: int,
        rules_id: int,
        action: WatchAction,
        interval_seconds: int,
        trigger_in_stock: bool = True,
        trigger_target_price: bool = True,
        expires_at: str | None = None,
        first_check_immediately: bool = True,
    ) -> WatchJobRecord:
        stamp = now_iso()
        next_check = stamp if first_check_immediately else iso_in(
            jittered_delay(interval_seconds)
        )
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO watch_jobs
                    (product_id, rules_id, status, action, trigger_in_stock,
                     trigger_target_price, interval_seconds, expires_at,
                     next_check_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    product_id,
                    rules_id,
                    WatchStatus.WATCHING.value,
                    action.value,
                    int(trigger_in_stock),
                    int(trigger_target_price),
                    max(MIN_CHECK_INTERVAL_SECONDS, int(interval_seconds)),
                    expires_at,
                    next_check,
                    stamp,
                    stamp,
                ),
            )
            job_id = int(cursor.lastrowid)
        record = self.get(job_id)
        if record is None:  # pragma: no cover
            raise RuntimeError("Watch job missing immediately after insert")
        logger.info(
            "Watch job created",
            extra={"watch_job_id": job_id, "action": action.value},
        )
        return record

    def update_settings(
        self,
        job_id: int,
        *,
        action: WatchAction | None = None,
        interval_seconds: int | None = None,
        trigger_in_stock: bool | None = None,
        trigger_target_price: bool | None = None,
        expires_at: str | None = None,
        clear_expiry: bool = False,
    ) -> None:
        assignments: list[str] = []
        params: list[object] = []
        if action is not None:
            assignments.append("action = ?")
            params.append(action.value)
        if interval_seconds is not None:
            assignments.append("interval_seconds = ?")
            params.append(max(MIN_CHECK_INTERVAL_SECONDS, int(interval_seconds)))
        if trigger_in_stock is not None:
            assignments.append("trigger_in_stock = ?")
            params.append(int(trigger_in_stock))
        if trigger_target_price is not None:
            assignments.append("trigger_target_price = ?")
            params.append(int(trigger_target_price))
        if clear_expiry:
            assignments.append("expires_at = NULL")
        elif expires_at is not None:
            assignments.append("expires_at = ?")
            params.append(expires_at)
        if not assignments:
            return
        assignments.append("updated_at = ?")
        params.extend([now_iso(), job_id])
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE watch_jobs SET {', '.join(assignments)} WHERE id = ?",
                tuple(params),
            )

    def set_status(
        self,
        job_id: int,
        status: WatchStatus,
        *,
        summary: str | None = None,
        error_code: str | None = None,
        paused_reason: str | None = None,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """
                UPDATE watch_jobs SET
                    status = ?,
                    last_check_summary = COALESCE(?, last_check_summary),
                    last_error_code = ?,
                    paused_reason = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    summary,
                    error_code,
                    paused_reason,
                    now_iso(),
                    job_id,
                ),
            )

    def pause(self, job_id: int, reason: str = "Paused by you") -> None:
        self.set_status(job_id, WatchStatus.PAUSED, paused_reason=reason)

    def resume(self, job_id: int) -> None:
        """Resume a paused job and schedule its next check with jitter."""
        job = self.get(job_id)
        if job is None:
            return
        with self._db.transaction() as conn:
            conn.execute(
                """
                UPDATE watch_jobs SET
                    status = ?, paused_reason = NULL, consecutive_failures = 0,
                    last_error_code = NULL,
                    next_check_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    WatchStatus.WATCHING.value,
                    iso_in(jittered_delay(job.interval_seconds) * 0.1),
                    now_iso(),
                    job_id,
                ),
            )

    def delete(self, job_id: int) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM watch_jobs WHERE id = ?", (job_id,))

    # ---- scheduling ------------------------------------------------------

    def due_jobs(self, *, limit: int = 10) -> list[WatchJobRecord]:
        """Active jobs whose next check time has passed, oldest first."""
        rows = self._db.query_all(
            """
            SELECT * FROM watch_jobs
            WHERE next_check_at <= ?
              AND status IN ('watching', 'waiting_for_price', 'target_reached',
                             'out_of_stock', 'error')
            ORDER BY next_check_at ASC
            LIMIT ?
            """,
            (now_iso(), limit),
        )
        return [WatchJobRecord.from_row(row) for row in rows]

    def next_due_at(self) -> str | None:
        """When the earliest active job is next due, for the dashboard."""
        return self._db.query_scalar(
            """
            SELECT MIN(next_check_at) FROM watch_jobs
            WHERE status IN ('watching', 'waiting_for_price', 'target_reached',
                             'out_of_stock', 'error')
            """
        )

    def record_check_success(
        self, job_id: int, *, summary: str, status: WatchStatus
    ) -> None:
        """Mark a completed check and schedule the next one."""
        job = self.get(job_id)
        if job is None:
            return
        with self._db.transaction() as conn:
            conn.execute(
                """
                UPDATE watch_jobs SET
                    status = ?, last_checked_at = ?, last_check_summary = ?,
                    last_error_code = NULL, consecutive_failures = 0,
                    checks_performed = checks_performed + 1,
                    next_check_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    now_iso(),
                    summary,
                    iso_in(jittered_delay(job.interval_seconds)),
                    now_iso(),
                    job_id,
                ),
            )

    def record_check_failure(
        self,
        job_id: int,
        *,
        summary: str,
        error_code: str,
        status: WatchStatus = WatchStatus.ERROR,
    ) -> int:
        """Record a failed check, applying the backoff ladder.

        Returns the delay in seconds that was applied, so the caller can say
        so in the activity feed.
        """
        job = self.get(job_id)
        if job is None:
            return 0
        failures = job.consecutive_failures + 1
        delay = max(backoff_delay(failures), job.interval_seconds)
        with self._db.transaction() as conn:
            conn.execute(
                """
                UPDATE watch_jobs SET
                    status = ?, last_checked_at = ?, last_check_summary = ?,
                    last_error_code = ?, consecutive_failures = ?,
                    checks_performed = checks_performed + 1,
                    next_check_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    now_iso(),
                    summary,
                    error_code,
                    failures,
                    iso_in(delay),
                    now_iso(),
                    job_id,
                ),
            )
        return delay

    def defer(self, job_id: int, seconds: float) -> None:
        """Push a job's next check back, used for rate-limit backoff."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE watch_jobs SET next_check_at = ?, updated_at = ? WHERE id = ?",
                (iso_in(seconds), now_iso(), job_id),
            )

    def defer_all_active(self, seconds: float) -> int:
        """Push every active job back.

        Used when Amazon signals throttling: that is an address-level signal,
        so every watch job slows down, not just the one that saw it.
        """
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE watch_jobs SET next_check_at = ?, updated_at = ?
                WHERE status IN ('watching', 'waiting_for_price',
                                 'target_reached', 'out_of_stock', 'error')
                  AND next_check_at < ?
                """,
                (iso_in(seconds), now_iso(), iso_in(seconds)),
            )
        return cursor.rowcount or 0

    def expire_due(self) -> list[int]:
        """Mark jobs whose stop date has passed. Returns the affected ids."""
        rows = self._db.query_all(
            "SELECT id FROM watch_jobs WHERE expires_at IS NOT NULL "
            "AND expires_at <= ? AND status NOT IN ('expired', 'purchase_completed')",
            (now_iso(),),
        )
        ids = [int(row["id"]) for row in rows]
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE watch_jobs SET status = 'expired', updated_at = ? "
                f"WHERE id IN ({placeholders})",
                (now_iso(), *ids),
            )
        return ids

    def mark_purchase_completed(self, job_id: int) -> None:
        """Stop a watch job because its purchase succeeded.

        This is what prevents an auto-buy watch from ordering twice: once the
        purchase is confirmed the job leaves every active status, so the
        scheduler will never pick it up again.
        """
        self.set_status(
            job_id,
            WatchStatus.PURCHASE_COMPLETED,
            summary="Purchased",
        )

    def active_count(self) -> int:
        return int(
            self._db.query_scalar(
                """
                SELECT COUNT(*) FROM watch_jobs
                WHERE status IN ('watching', 'waiting_for_price',
                                 'target_reached', 'out_of_stock', 'error')
                """
            )
            or 0
        )
