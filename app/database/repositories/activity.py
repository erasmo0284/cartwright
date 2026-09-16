"""Activity feed, notification delivery log and automation diagnostics."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from app.core.money import Money
from app.core.timeutil import from_iso, now_iso
from app.database.database import Database
from app.database.records import ActivityCategory, ActivityEvent, ActivitySeverity
from app.diagnostics.redaction import redact_mapping, redact_url

logger = logging.getLogger("app.database.activity")

#: Activity rows retained. A year of monitoring at five-minute intervals
#: would be far more than a user will ever scroll, so the feed is capped and
#: the oldest entries are pruned.
ACTIVITY_RETENTION = 5_000


class ActivityRepository:
    """Appends to the activity feed and answers its filtered queries."""

    def __init__(self, database: Database) -> None:
        self._db = database

    # ---- activity --------------------------------------------------------

    def add(
        self,
        *,
        category: ActivityCategory,
        severity: ActivitySeverity,
        title: str,
        detail: str | None = None,
        product_id: int | None = None,
        watch_job_id: int | None = None,
        purchase_job_id: int | None = None,
        order_id: int | None = None,
        error_code: str | None = None,
        amount: Money | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        """Append an event. Metadata is redacted before it is stored."""
        safe_metadata = redact_mapping(metadata) if metadata else None
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO activity_events (
                    created_at, category, severity, title, detail, product_id,
                    watch_job_id, purchase_job_id, order_id, error_code,
                    amount_cents, currency, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_iso(),
                    category.value,
                    severity.value,
                    title,
                    detail,
                    product_id,
                    watch_job_id,
                    purchase_job_id,
                    order_id,
                    error_code,
                    amount.cents if amount else None,
                    amount.currency if amount else None,
                    json.dumps(safe_metadata) if safe_metadata else None,
                ),
            )
            event_id = int(cursor.lastrowid)
        return event_id

    def prune(self) -> int:
        """Delete the oldest events beyond the retention limit."""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                DELETE FROM activity_events
                WHERE id NOT IN (
                    SELECT id FROM activity_events
                    ORDER BY created_at DESC, id DESC LIMIT ?
                )
                """,
                (ACTIVITY_RETENTION,),
            )
        return cursor.rowcount or 0

    def list_events(
        self,
        *,
        category: ActivityCategory | None = None,
        product_id: int | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[ActivityEvent]:
        clauses: list[str] = []
        params: list[Any] = []
        if category is not None:
            clauses.append("category = ?")
            params.append(category.value)
        if product_id is not None:
            clauses.append("product_id = ?")
            params.append(product_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        rows = self._db.query_all(
            f"SELECT * FROM activity_events {where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            tuple(params),
        )
        return [ActivityEvent.from_row(row) for row in rows]

    def recent_for_dashboard(self, *, limit: int = 6) -> list[ActivityEvent]:
        """A short, deliberately mixed slice for the dashboard panel."""
        return self.list_events(limit=limit)

    def count(self, *, category: ActivityCategory | None = None) -> int:
        if category is None:
            return int(
                self._db.query_scalar("SELECT COUNT(*) FROM activity_events") or 0
            )
        return int(
            self._db.query_scalar(
                "SELECT COUNT(*) FROM activity_events WHERE category = ?",
                (category.value,),
            )
            or 0
        )

    def clear(self) -> int:
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM activity_events")
        return cursor.rowcount or 0

    # ---- notifications ---------------------------------------------------

    def was_notified_recently(
        self, dedupe_key: str, *, within_seconds: int
    ) -> bool:
        """Whether an equivalent notification was delivered recently.

        Stops a watch job that stays over target from notifying on every
        single check.
        """
        row = self._db.query_one(
            "SELECT created_at FROM notification_events "
            "WHERE dedupe_key = ? AND delivered = 1 "
            "ORDER BY created_at DESC LIMIT 1",
            (dedupe_key,),
        )
        if row is None:
            return False
        moment = from_iso(row["created_at"])
        if moment is None:
            return False
        from app.core.timeutil import utcnow

        return (utcnow() - moment).total_seconds() < within_seconds

    def record_notification(
        self,
        *,
        kind: str,
        dedupe_key: str,
        title: str,
        body: str | None,
        delivered: bool,
        delivery_method: str | None = None,
        suppressed_why: str | None = None,
    ) -> int:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO notification_events
                    (kind, dedupe_key, title, body, delivered, delivery_method,
                     suppressed_why, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    dedupe_key,
                    title,
                    body,
                    int(delivered),
                    delivery_method,
                    suppressed_why,
                    now_iso(),
                ),
            )
            return int(cursor.lastrowid)

    # ---- automation diagnostics ------------------------------------------

    def record_diagnostic(
        self,
        *,
        step: str,
        error_code: str | None,
        page_url: str | None,
        screenshot_file: str | None,
        metadata: Mapping[str, Any] | None = None,
        purchase_job_id: int | None = None,
        watch_job_id: int | None = None,
    ) -> int:
        """Store a pointer to a saved diagnostic capture.

        The URL is reduced by :func:`redact_url` and the metadata by
        :func:`redact_mapping` before storage, so a diagnostics row can never
        hold a session token.
        """
        safe_metadata = redact_mapping(metadata) if metadata else None
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO automation_diagnostics
                    (created_at, step, error_code, page_url, screenshot_file,
                     metadata_json, purchase_job_id, watch_job_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_iso(),
                    step,
                    error_code,
                    redact_url(page_url) if page_url else None,
                    screenshot_file,
                    json.dumps(safe_metadata) if safe_metadata else None,
                    purchase_job_id,
                    watch_job_id,
                ),
            )
            return int(cursor.lastrowid)

    def list_diagnostics(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.query_all(
            "SELECT * FROM automation_diagnostics ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    def clear_diagnostics(self) -> list[str]:
        """Forget every diagnostic row, returning the filenames to delete."""
        rows = self._db.query_all(
            "SELECT screenshot_file FROM automation_diagnostics "
            "WHERE screenshot_file IS NOT NULL"
        )
        files = [str(row["screenshot_file"]) for row in rows]
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM automation_diagnostics")
        return files
