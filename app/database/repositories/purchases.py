"""Purchase jobs, their state transitions, attempts and guard reports.

This repository holds the duplicate-order protections. Three of them are
enforced by SQLite itself (see :mod:`app.database.migrations.m0001_initial`);
this module adds the two that need application logic:

* :meth:`PurchaseRepository.recover_interrupted` -- on startup, any job that
  was mid-submission when the process died is moved to ``UNKNOWN``, never
  resumed. An order may exist, and only a human can say.
* :meth:`PurchaseRepository.mark_submitted` -- turns the database's
  one-submission-per-job index into a typed refusal, so the caller cannot
  mistake it for a transient error and retry.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from dataclasses import dataclass

from app.core.errors import AppError, ErrorCode
from app.core.timeutil import now_iso
from app.database.database import Database
from app.database.records import PurchaseJobRecord
from app.purchasing.models import CartStrategy, PurchaseMode
from app.purchasing.states import (
    SUBMIT_ENTRY_STATES,
    IllegalTransition,
    PurchaseState,
    assert_transition,
)
from app.purchasing.validation import GuardPhase, GuardReport

logger = logging.getLogger("app.database.purchases")


@dataclass(frozen=True)
class PurchaseAttempt:
    """One run of the checkout sequence for a purchase job."""

    id: int
    purchase_job_id: int
    attempt_number: int
    submit_token: str
    submitted: bool
    outcome: str | None = None
    detail: str | None = None


class PurchaseRepository:
    """Stores purchase jobs and guarantees a job submits at most once."""

    def __init__(self, database: Database) -> None:
        self._db = database

    # ---- creation --------------------------------------------------------

    def create(
        self,
        *,
        product_id: int,
        rules_id: int,
        mode: PurchaseMode,
        test_mode: bool,
        watch_job_id: int | None = None,
    ) -> PurchaseJobRecord:
        """Start a purchase job.

        Raises :class:`AppError` with :attr:`ErrorCode.DUPLICATE_BLOCKED` when
        the product already has a live purchase job. That includes a job left
        in ``UNKNOWN``, so an unresolved outcome blocks any new attempt until
        the user says what happened.
        """
        stamp = now_iso()
        key = secrets.token_hex(16)
        try:
            with self._db.transaction() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO purchase_jobs
                        (product_id, rules_id, watch_job_id, mode, test_mode,
                         state, idempotency_key, created_at, updated_at,
                         state_changed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        product_id,
                        rules_id,
                        watch_job_id,
                        mode.value,
                        int(test_mode),
                        PurchaseState.CREATED.value,
                        key,
                        stamp,
                        stamp,
                        stamp,
                    ),
                )
                job_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            existing = self.live_job_for_product(product_id)
            logger.warning(
                "Refused a second purchase job for a product",
                extra={
                    "product_id": product_id,
                    "existing_job_id": existing.id if existing else None,
                    "existing_state": existing.state.value if existing else None,
                },
            )
            raise AppError(
                ErrorCode.DUPLICATE_BLOCKED,
                context={
                    "product_id": product_id,
                    "existing_state": existing.state.value if existing else "unknown",
                },
                detail_override=(
                    "This item already has a purchase in progress, so a second "
                    "one was not started."
                    if existing is None or existing.state is not PurchaseState.UNKNOWN
                    else (
                        "A previous order for this item has an unconfirmed result. "
                        "Check your Amazon orders and resolve it before trying "
                        "again."
                    )
                ),
                cause=exc,
            ) from exc

        self._record_transition(job_id, None, PurchaseState.CREATED, "created")
        record = self.get(job_id)
        if record is None:  # pragma: no cover
            raise RuntimeError("Purchase job missing immediately after insert")
        logger.info(
            "Purchase job created",
            extra={
                "purchase_job_id": job_id,
                "mode": mode.value,
                "test_mode": test_mode,
                "watch_job_id": watch_job_id,
            },
        )
        return record

    # ---- reading ---------------------------------------------------------

    def get(self, job_id: int) -> PurchaseJobRecord | None:
        row = self._db.query_one(
            "SELECT * FROM purchase_jobs WHERE id = ?", (job_id,)
        )
        return PurchaseJobRecord.from_row(row) if row else None

    def live_job_for_product(self, product_id: int) -> PurchaseJobRecord | None:
        """The job currently occupying this product's purchase slot, if any."""
        placeholders = ", ".join("?" for _ in _LIVE_VALUES)
        row = self._db.query_one(
            f"SELECT * FROM purchase_jobs WHERE product_id = ? "
            f"AND state IN ({placeholders}) LIMIT 1",
            (product_id, *_LIVE_VALUES),
        )
        return PurchaseJobRecord.from_row(row) if row else None

    def list_recent(self, *, limit: int = 50) -> list[PurchaseJobRecord]:
        rows = self._db.query_all(
            "SELECT * FROM purchase_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [PurchaseJobRecord.from_row(row) for row in rows]

    def list_needing_attention(self) -> list[PurchaseJobRecord]:
        """Jobs waiting on the user, uncertain results first."""
        rows = self._db.query_all(
            "SELECT * FROM purchase_jobs "
            "WHERE state IN ('unknown', 'needs_user', 'awaiting_confirmation') "
            "ORDER BY CASE state WHEN 'unknown' THEN 0 WHEN 'needs_user' THEN 1 "
            "ELSE 2 END, updated_at DESC"
        )
        return [PurchaseJobRecord.from_row(row) for row in rows]

    def has_completed_purchase(self, watch_job_id: int) -> bool:
        """Whether a watch job has already produced a confirmed purchase."""
        count = self._db.query_scalar(
            "SELECT COUNT(*) FROM purchase_jobs "
            "WHERE watch_job_id = ? AND state = 'confirmed'",
            (watch_job_id,),
        )
        return bool(count)

    # ---- state transitions ----------------------------------------------

    def transition(
        self,
        job_id: int,
        to_state: PurchaseState,
        *,
        reason: str | None = None,
        outcome_code: str | None = None,
        outcome_detail: str | None = None,
    ) -> PurchaseJobRecord:
        """Move a job to ``to_state``, refusing any move the design forbids.

        Re-entering the current state is a no-op rather than an error, so a
        second verification prompt does not clutter the audit trail.
        """
        job = self.get(job_id)
        if job is None:
            raise KeyError(f"No purchase job with id {job_id}")
        if job.state is to_state:
            return job

        assert_transition(job.state, to_state)

        stamp = now_iso()
        terminal = stamp if to_state.is_terminal else None
        with self._db.transaction() as conn:
            # The WHERE clause re-checks the state we validated against, so a
            # concurrent transition cannot be silently overwritten.
            cursor = conn.execute(
                """
                UPDATE purchase_jobs SET
                    state = ?, state_changed_at = ?, updated_at = ?,
                    terminal_at = COALESCE(?, terminal_at),
                    outcome_code = COALESCE(?, outcome_code),
                    outcome_detail = COALESCE(?, outcome_detail)
                WHERE id = ? AND state = ?
                """,
                (
                    to_state.value,
                    stamp,
                    stamp,
                    terminal,
                    outcome_code,
                    outcome_detail,
                    job_id,
                    job.state.value,
                ),
            )
            if cursor.rowcount == 0:
                raise IllegalTransition(job.state, to_state)
            conn.execute(
                """
                INSERT INTO purchase_state_transitions
                    (purchase_job_id, from_state, to_state, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, job.state.value, to_state.value, reason, stamp),
            )

        logger.info(
            "Purchase state changed",
            extra={
                "purchase_job_id": job_id,
                "from_state": job.state.value,
                "to_state": to_state.value,
                "reason": reason,
            },
        )
        updated = self.get(job_id)
        if updated is None:  # pragma: no cover
            raise RuntimeError("Purchase job vanished during transition")
        return updated

    def _record_transition(
        self,
        job_id: int,
        from_state: PurchaseState | None,
        to_state: PurchaseState,
        reason: str,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO purchase_state_transitions
                    (purchase_job_id, from_state, to_state, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    from_state.value if from_state else None,
                    to_state.value,
                    reason,
                    now_iso(),
                ),
            )

    def transitions(self, job_id: int) -> list[dict[str, str | None]]:
        rows = self._db.query_all(
            "SELECT from_state, to_state, reason, created_at "
            "FROM purchase_state_transitions WHERE purchase_job_id = ? ORDER BY id",
            (job_id,),
        )
        return [
            {
                "from_state": row["from_state"],
                "to_state": row["to_state"],
                "reason": row["reason"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def set_cart_strategy(self, job_id: int, strategy: CartStrategy) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE purchase_jobs SET cart_strategy = ?, updated_at = ? "
                "WHERE id = ?",
                (strategy.value, now_iso(), job_id),
            )

    def set_isolation_journal(self, job_id: int, journal_json: str | None) -> None:
        """Persist what was set aside, so it can be restored after a crash."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE purchase_jobs SET isolation_journal_json = ?, "
                "updated_at = ? WHERE id = ?",
                (journal_json, now_iso(), job_id),
            )

    # ---- attempts --------------------------------------------------------

    def begin_attempt(self, job_id: int) -> PurchaseAttempt:
        """Open an attempt row. Opening one does not submit anything."""
        next_number = int(
            self._db.query_scalar(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM purchase_attempts "
                "WHERE purchase_job_id = ?",
                (job_id,),
            )
            or 1
        )
        token = secrets.token_hex(16)
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO purchase_attempts
                    (purchase_job_id, attempt_number, submit_token, submitted,
                     started_at)
                VALUES (?, ?, ?, 0, ?)
                """,
                (job_id, next_number, token, now_iso()),
            )
            attempt_id = int(cursor.lastrowid)
        return PurchaseAttempt(
            id=attempt_id,
            purchase_job_id=job_id,
            attempt_number=next_number,
            submit_token=token,
            submitted=False,
        )

    def mark_submitted(self, attempt: PurchaseAttempt) -> None:
        """Record that the order button is about to be clicked.

        Called immediately *before* the click, never after, so a crash during
        the click still leaves evidence that a submission happened. The
        database permits one submitted attempt per job; a second call raises
        :class:`AppError` with :attr:`ErrorCode.DUPLICATE_BLOCKED` rather
        than a generic database error, so it can never be mistaken for a
        transient failure worth retrying.
        """
        job = self.get(attempt.purchase_job_id)
        if job is None:
            raise KeyError(f"No purchase job with id {attempt.purchase_job_id}")
        if job.state not in SUBMIT_ENTRY_STATES:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                context={
                    "reason": "submission_from_unexpected_state",
                    "state": job.state.value,
                },
                detail_override=(
                    "The order was not submitted because the app was not in a "
                    "state where submitting is allowed."
                ),
            )
        try:
            with self._db.transaction() as conn:
                cursor = conn.execute(
                    "UPDATE purchase_attempts SET submitted = 1, submitted_at = ? "
                    "WHERE id = ? AND submitted = 0",
                    (now_iso(), attempt.id),
                )
                if cursor.rowcount == 0:
                    # Already marked submitted: the update matched nothing, so
                    # no IntegrityError was raised. Refuse explicitly rather
                    # than returning as though the mark had just been made.
                    raise sqlite3.IntegrityError(
                        "attempt was already marked as submitted"
                    )
        except sqlite3.IntegrityError as exc:
            logger.error(
                "Refused a second submission for a purchase job",
                extra={
                    "purchase_job_id": attempt.purchase_job_id,
                    "attempt_id": attempt.id,
                },
            )
            raise AppError(
                ErrorCode.DUPLICATE_BLOCKED,
                context={"purchase_job_id": attempt.purchase_job_id},
                detail_override=(
                    "This purchase has already been submitted once, so it was "
                    "not submitted again."
                ),
                cause=exc,
            ) from exc
        logger.info(
            "Purchase submission recorded",
            extra={
                "purchase_job_id": attempt.purchase_job_id,
                "attempt_id": attempt.id,
            },
        )

    def finish_attempt(
        self, attempt: PurchaseAttempt, *, outcome: str, detail: str | None = None
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE purchase_attempts SET finished_at = ?, outcome = ?, "
                "detail = ? WHERE id = ?",
                (now_iso(), outcome, detail, attempt.id),
            )

    def submitted_attempt(self, job_id: int) -> PurchaseAttempt | None:
        row = self._db.query_one(
            "SELECT * FROM purchase_attempts WHERE purchase_job_id = ? "
            "AND submitted = 1 LIMIT 1",
            (job_id,),
        )
        if row is None:
            return None
        return PurchaseAttempt(
            id=int(row["id"]),
            purchase_job_id=int(row["purchase_job_id"]),
            attempt_number=int(row["attempt_number"]),
            submit_token=str(row["submit_token"]),
            submitted=True,
            outcome=row["outcome"],
            detail=row["detail"],
        )

    def has_submitted(self, job_id: int) -> bool:
        return self.submitted_attempt(job_id) is not None

    # ---- guard reports ---------------------------------------------------

    def save_guard_report(self, job_id: int, report: GuardReport) -> int:
        blocked = report.blocked_code
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO guard_reports
                    (purchase_job_id, phase, passed, blocked_code, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    report.phase.value,
                    int(report.passed),
                    blocked.value if blocked else None,
                    now_iso(),
                ),
            )
            report_id = int(cursor.lastrowid)
            for position, check in enumerate(report.checks):
                conn.execute(
                    """
                    INSERT INTO guard_checks
                        (report_id, check_id, title, status, expected, actual,
                         error_code, required, position)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report_id,
                        check.check_id,
                        check.title,
                        check.status.value,
                        check.expected,
                        check.actual,
                        check.error_code.value if check.error_code else None,
                        int(check.required),
                        position,
                    ),
                )
        return report_id

    def latest_guard_report(
        self, job_id: int, phase: GuardPhase | None = None
    ) -> dict[str, object] | None:
        """The most recent stored report, as plain data for the UI."""
        if phase is None:
            row = self._db.query_one(
                "SELECT * FROM guard_reports WHERE purchase_job_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (job_id,),
            )
        else:
            row = self._db.query_one(
                "SELECT * FROM guard_reports WHERE purchase_job_id = ? AND phase = ? "
                "ORDER BY id DESC LIMIT 1",
                (job_id, phase.value),
            )
        if row is None:
            return None
        checks = self._db.query_all(
            "SELECT check_id, title, status, expected, actual, error_code, required "
            "FROM guard_checks WHERE report_id = ? ORDER BY position",
            (int(row["id"]),),
        )
        return {
            "phase": row["phase"],
            "passed": bool(row["passed"]),
            "blocked_code": row["blocked_code"],
            "created_at": row["created_at"],
            "checks": [dict(check) for check in checks],
        }

    # ---- crash recovery --------------------------------------------------

    def recover_interrupted(self) -> list[int]:
        """Resolve jobs that were interrupted by a crash or a forced exit.

        Called once at startup. Jobs that had not yet submitted are failed --
        nothing was ordered, so it is safe to say so. Jobs that were
        submitting or confirming become ``UNKNOWN``: an order may exist on
        Amazon, and the program must never guess, nor retry.

        Returns the ids of jobs moved to ``UNKNOWN``, so the UI can raise
        them with the user immediately.
        """
        uncertain: list[int] = []

        for row in self._db.query_all(
            "SELECT * FROM purchase_jobs WHERE state IN ('submitting', 'confirming')"
        ):
            job = PurchaseJobRecord.from_row(row)
            self.transition(
                job.id,
                PurchaseState.UNKNOWN,
                reason="the application stopped while the order was being placed",
                outcome_code=ErrorCode.ORDER_RESULT_UNCERTAIN.value,
                outcome_detail=(
                    "The app stopped while this order was being placed, so it "
                    "cannot tell whether Amazon accepted it. It will not try "
                    "again."
                ),
            )
            uncertain.append(job.id)

        # Anything earlier in the sequence never reached a submission. The
        # submitted-attempt check is belt and braces: if an attempt was
        # somehow marked submitted, treat the job as uncertain instead.
        for row in self._db.query_all(
            "SELECT * FROM purchase_jobs WHERE state IN "
            "('created', 'product_check', 'rule_validation', 'cart_preparation', "
            "'checkout', 'final_validation', 'awaiting_confirmation')"
        ):
            job = PurchaseJobRecord.from_row(row)
            if self.has_submitted(job.id):
                self.transition(
                    job.id,
                    PurchaseState.FAILED,
                    reason="interrupted after a recorded submission",
                    outcome_code=ErrorCode.ORDER_RESULT_UNCERTAIN.value,
                    outcome_detail=(
                        "This purchase was interrupted after it had been "
                        "submitted. Check your Amazon orders."
                    ),
                )
                continue
            self.transition(
                job.id,
                PurchaseState.FAILED,
                reason="the application stopped before the order was placed",
                outcome_code=ErrorCode.CANCELLED.value,
                outcome_detail="The app stopped before this order was placed. "
                "Nothing was ordered.",
            )

        if uncertain:
            logger.warning(
                "Purchases with an uncertain outcome were found at startup",
                extra={"purchase_job_ids": uncertain},
            )
        return uncertain

    def resolve_unknown(
        self, job_id: int, *, order_was_placed: bool, note: str
    ) -> PurchaseJobRecord:
        """Close out an uncertain purchase using the user's own answer.

        The only way out of ``UNKNOWN``. There is deliberately no automatic
        resolution: the program cannot tell, so it asks.
        """
        job = self.get(job_id)
        if job is None:
            raise KeyError(f"No purchase job with id {job_id}")
        if job.state is not PurchaseState.UNKNOWN:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                context={"reason": "not_uncertain", "state": job.state.value},
                detail_override="This purchase does not need resolving.",
            )
        target = (
            PurchaseState.CONFIRMED if order_was_placed else PurchaseState.FAILED
        )
        return self.transition(
            job_id,
            target,
            reason="resolved by the user",
            outcome_detail=note,
        )


_LIVE_VALUES: tuple[str, ...] = (
    PurchaseState.CREATED.value,
    PurchaseState.PRODUCT_CHECK.value,
    PurchaseState.RULE_VALIDATION.value,
    PurchaseState.CART_PREPARATION.value,
    PurchaseState.CHECKOUT.value,
    PurchaseState.FINAL_VALIDATION.value,
    PurchaseState.AWAITING_CONFIRMATION.value,
    PurchaseState.SUBMITTING.value,
    PurchaseState.CONFIRMING.value,
    PurchaseState.NEEDS_USER.value,
    PurchaseState.UNKNOWN.value,
)
