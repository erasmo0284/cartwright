"""The monitoring service: runs watch checks and acts on what they find.

Responsibilities, in order of importance:

1. Never check more often than configured, and slow down when Amazon says to.
   A throttle signal is treated as an address-level condition, so *every*
   watch job backs off, not just the one that saw it.
2. Record every observation, so the watch list and price history are honest
   about when a price was last confirmed.
3. Hand an automatic purchase to the purchase service, which re-runs the full
   Purchase Guard. This service never decides to buy on its own.
4. Stop rather than loop when the session expires or Amazon asks for a human.

Monitoring continues while the window is closed -- that is the point of the
tray -- so everything here has to behave well unattended.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from app.automation.amazon_adapter import ADAPTER
from app.automation.browser_worker import BrowserSession, BrowserWorker, Priority
from app.config import NotificationKind, SettingsService
from app.core.errors import AppError, ErrorCode
from app.database.records import (
    ActivityCategory,
    ActivitySeverity,
    WatchAction,
    WatchJobView,
    WatchStatus,
)
from app.database.repositories import (
    ActivityRepository,
    ProductRepository,
    PurchaseRepository,
    RulesRepository,
    WatchRepository,
)
from app.monitoring.scheduler import MAX_JOBS_PER_TICK, RequestPacer, Scheduler
from app.monitoring.watcher import CheckDecision, evaluate_check
from app.purchasing.models import (
    ProductSnapshot,
    PurchaseMode,
    snapshot_to_metadata,
)
from app.purchasing.purchase_service import PurchaseService

logger = logging.getLogger("app.monitoring.service")

#: How long to wait before re-checking anything after a throttle signal.
THROTTLE_BACKOFF_SECONDS = 30 * 60

#: How long to suppress a repeat of the same notification.
NOTIFICATION_DEDUPE_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class CheckResult:
    """What one completed check produced."""

    watch_job_id: int
    snapshot: ProductSnapshot
    decision: CheckDecision


class MonitorService(QObject):
    """Drives the watch list."""

    #: Emitted after any change the watch list should redraw for.
    watchlist_changed = Signal()
    #: watch_job_id, step message
    progress = Signal(int, str)
    #: True when monitoring is running.
    monitoring_changed = Signal(bool)
    #: A :class:`CheckResult`.
    check_completed = Signal(object)
    #: watch_job_id, AppError
    check_failed = Signal(int, object)

    def __init__(
        self,
        *,
        worker: BrowserWorker,
        scheduler: Scheduler,
        watches: WatchRepository,
        products: ProductRepository,
        rules_repo: RulesRepository,
        purchases: PurchaseRepository,
        activity: ActivityRepository,
        settings: SettingsService,
        purchase_service: PurchaseService,
        notifier: object | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._worker = worker
        self._scheduler = scheduler
        self._watches = watches
        self._products = products
        self._rules_repo = rules_repo
        self._purchases = purchases
        self._activity = activity
        self._settings = settings
        self._purchase_service = purchase_service
        self._notifier = notifier
        self._pacer = RequestPacer()
        self._in_flight: dict[int, int] = {}

        scheduler.tick.connect(self._on_tick)
        scheduler.resumed.connect(self._on_resumed)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_cancelled)

    # ---- control ---------------------------------------------------------

    @property
    def is_monitoring(self) -> bool:
        return self._scheduler.is_running

    def start(self) -> None:
        if not self._settings.current.monitoring_enabled:
            logger.info("Monitoring is switched off in settings")
            return
        self._scheduler.start()
        self.monitoring_changed.emit(True)

    def stop(self) -> None:
        self._scheduler.stop()
        self._worker.cancel_all(only_priority=Priority.MONITORING)
        self.monitoring_changed.emit(False)

    def set_enabled(self, enabled: bool) -> None:
        """Turn monitoring on or off and remember the choice."""
        self._settings.update(monitoring_enabled=enabled)
        if enabled:
            self.start()
        else:
            self.stop()

    def check_now(self, watch_job_id: int | None = None) -> None:
        """Check one job, or every active job, straight away.

        A manual request runs at interactive priority so the user is not
        queued behind background work, and it bypasses the pacing gap because
        a person is waiting for it.
        """
        if watch_job_id is not None:
            view = self._watches.view(watch_job_id)
            if view is not None:
                self._dispatch(view, priority=Priority.INTERACTIVE)
            return
        for view in self._watches.list_views(include_finished=False):
            if view.job.status.is_active:
                self._dispatch(view, priority=Priority.INTERACTIVE)

    # ---- the tick --------------------------------------------------------

    def _on_tick(self) -> None:
        expired = self._watches.expire_due()
        if expired:
            logger.info("Watch jobs reached their stop date", extra={"ids": expired})
            self.watchlist_changed.emit()

        pacing = self._pacer.check()
        if not pacing.allowed:
            return

        due = self._watches.due_jobs(limit=MAX_JOBS_PER_TICK)
        if not due:
            return

        for record in due:
            if record.id in self._in_flight:
                continue
            if not self._pacer.check().allowed:
                break
            view = self._watches.view(record.id)
            if view is None:
                continue
            self._dispatch(view, priority=Priority.MONITORING)

    def _on_resumed(self, jump_seconds: float) -> None:
        """Re-anchor after sleep instead of firing every missed check.

        Each job's next check is pushed a short, staggered distance into the
        future so a machine waking from sleep does not immediately load a
        dozen pages on a network that may not be ready.
        """
        views = [
            view
            for view in self._watches.list_views(include_finished=False)
            if view.job.status.is_active
        ]
        self._pacer.reset()
        for index, view in enumerate(views):
            self._watches.defer(view.job.id, 15 + index * 20)
        logger.info(
            "Rescheduled watch jobs after a time jump",
            extra={"jobs": len(views), "jump_seconds": round(jump_seconds, 1)},
        )
        if views:
            self.watchlist_changed.emit()

    def _dispatch(self, view: WatchJobView, *, priority: Priority) -> None:
        job_id = view.job.id
        if job_id in self._in_flight:
            return
        self._pacer.record_request()
        task_id = self._worker.submit(
            f"Check {view.product.display_title}",
            lambda session: self._run_check(session, view),
            priority=priority,
            context={"kind": "watch", "watch_job_id": job_id},
        )
        self._in_flight[job_id] = task_id

    # ---- the check -------------------------------------------------------

    def _run_check(
        self, session: BrowserSession, view: WatchJobView
    ) -> CheckResult:
        """Check one product. Runs on the browser thread."""
        snapshot = ADAPTER.inspect_product(
            session,
            asin=view.rules.expected_asin,
            marketplace=view.product.marketplace,
        )
        previous = self._products.latest_observation(view.product.id)

        self._products.upsert_from_snapshot(snapshot)
        self._products.record_observation(
            view.product.id, snapshot, watch_job_id=view.job.id
        )
        self._products.record_variation(view.product.id, snapshot.variation)

        decision = evaluate_check(
            rules=view.rules,
            snapshot=snapshot,
            previous=previous,
            action=view.job.action,
            trigger_in_stock=view.job.trigger_in_stock,
            trigger_target_price=view.job.trigger_target_price,
        )
        self._watches.record_check_success(
            view.job.id, summary=decision.summary, status=decision.status
        )
        return CheckResult(
            watch_job_id=view.job.id, snapshot=snapshot, decision=decision
        )

    # ---- results ---------------------------------------------------------

    def _on_finished(self, task_id: int, result: object, context: object) -> None:
        watch_job_id = self._watch_id(context)
        if watch_job_id is None:
            return
        self._in_flight.pop(watch_job_id, None)
        if not isinstance(result, CheckResult):
            return

        view = self._watches.view(watch_job_id)
        if view is None:
            return
        decision = result.decision

        self._activity.add(
            category=ActivityCategory.WATCH_CHECK,
            severity=ActivitySeverity.INFO,
            title=f"Checked {view.product.display_title}",
            detail=decision.summary,
            product_id=view.product.id,
            watch_job_id=watch_job_id,
            amount=result.snapshot.price,
            metadata=snapshot_to_metadata(result.snapshot),
        )

        if decision.seller_changed:
            self._activity.add(
                category=ActivityCategory.WARNING,
                severity=ActivitySeverity.WARNING,
                title=f"Seller changed: {view.product.display_title}",
                detail=(
                    f"Expected {view.rules.expected_seller}, "
                    f"found {result.snapshot.seller or 'no seller shown'}"
                ),
                product_id=view.product.id,
                watch_job_id=watch_job_id,
                error_code=ErrorCode.SELLER_CHANGED.value,
            )

        self._send_notifications(view, result)
        self.check_completed.emit(result)
        self.watchlist_changed.emit()

        if decision.should_buy:
            self._start_automatic_purchase(view)

    def _start_automatic_purchase(self, view: WatchJobView) -> None:
        """Hand an automatic purchase to the purchase service.

        Guarded three times over: the watch must already have no confirmed
        purchase, the product must have no live purchase job (enforced by the
        database), and the purchase service re-runs the whole guard. This
        method does not decide to buy; it only asks.
        """
        job_id = view.job.id
        if self._purchases.has_completed_purchase(job_id):
            logger.info(
                "Watch already produced a purchase; not buying again",
                extra={"watch_job_id": job_id},
            )
            self._watches.mark_purchase_completed(job_id)
            return
        existing = self._purchases.live_job_for_product(view.product.id)
        if existing is not None:
            logger.info(
                "A purchase for this product is already in progress",
                extra={
                    "watch_job_id": job_id,
                    "existing_job": existing.id,
                    "state": existing.state.value,
                },
            )
            return

        try:
            self._purchase_service.start(
                product_id=view.product.id,
                rules=view.rules,
                mode=PurchaseMode.AUTOMATIC,
                allow_set_aside=False,
                watch_job_id=job_id,
            )
        except AppError as error:
            logger.warning(
                "Automatic purchase could not start",
                extra={"watch_job_id": job_id, "code": error.code.value},
            )
            self._activity.add(
                category=ActivityCategory.WARNING,
                severity=ActivitySeverity.WARNING,
                title=f"Automatic purchase not started: {view.product.display_title}",
                detail=error.detail,
                product_id=view.product.id,
                watch_job_id=job_id,
                error_code=error.code.value,
            )

    def _send_notifications(self, view: WatchJobView, result: CheckResult) -> None:
        if self._notifier is None:
            return
        from app.notifications import notifier as builders

        decision = result.decision
        title = view.product.display_title

        for kind in decision.notifications:
            request = None
            if kind is NotificationKind.TARGET_PRICE_REACHED and (
                result.snapshot.price and view.rules.max_item_price
            ):
                request = builders.target_price_reached(
                    title,
                    result.snapshot.price,
                    view.rules.max_item_price,
                    view.job.id,
                )
            elif kind is NotificationKind.BACK_IN_STOCK:
                request = builders.back_in_stock(title, view.job.id)
            elif kind is NotificationKind.SELLER_CHANGED:
                request = builders.seller_changed(
                    title,
                    view.rules.expected_seller or "the original seller",
                    result.snapshot.seller or "no seller shown",
                    view.job.id,
                )
            if request is not None:
                self._notifier.notify(request)

    # ---- failures --------------------------------------------------------

    def _on_failed(self, task_id: int, error: object, context: object) -> None:
        watch_job_id = self._watch_id(context)
        if watch_job_id is None or not isinstance(error, AppError):
            return
        self._in_flight.pop(watch_job_id, None)

        view = self._watches.view(watch_job_id)
        status = self._status_for_error(error)
        delay = self._watches.record_check_failure(
            watch_job_id,
            summary=error.title,
            error_code=error.code.value,
            status=status,
        )

        if error.code is ErrorCode.RATE_LIMITED:
            # Amazon throttles by address, so slowing one job down would not
            # help. Everything backs off.
            affected = self._watches.defer_all_active(THROTTLE_BACKOFF_SECONDS)
            logger.warning(
                "Amazon asked us to slow down; every watch backed off",
                extra={"jobs": affected},
            )
        elif error.code is ErrorCode.VERIFICATION_REQUIRED:
            # Never poll into a challenge. Monitoring pauses until the person
            # clears it, which they are told about.
            self.stop()
            logger.warning("Monitoring paused: Amazon needs a human")

        self._activity.add(
            category=(
                ActivityCategory.WARNING if error.needs_user else ActivityCategory.ERROR
            ),
            severity=(
                ActivitySeverity.WARNING if error.needs_user else ActivitySeverity.ERROR
            ),
            title=(
                f"Check failed: {view.product.display_title}"
                if view
                else "Check failed"
            ),
            detail=error.detail,
            product_id=view.product.id if view else None,
            watch_job_id=watch_job_id,
            error_code=error.code.value,
            metadata={"next_check_in_seconds": delay},
        )

        if self._notifier is not None and view is not None:
            from app.notifications import notifier as builders

            if error.code is ErrorCode.LOGIN_EXPIRED:
                self._notifier.notify(builders.login_expired())
            elif error.code is ErrorCode.VERIFICATION_REQUIRED:
                self._notifier.notify(builders.verification_needed())
            else:
                self._notifier.notify(
                    builders.monitoring_error(view.product.display_title, error.title)
                )

        self.check_failed.emit(watch_job_id, error)
        self.watchlist_changed.emit()

    @staticmethod
    def _status_for_error(error: AppError) -> WatchStatus:
        return {
            ErrorCode.LOGIN_EXPIRED: WatchStatus.NEEDS_LOGIN,
            ErrorCode.SESSION_INVALID: WatchStatus.NEEDS_LOGIN,
            ErrorCode.NOT_CONNECTED: WatchStatus.NEEDS_LOGIN,
            ErrorCode.VERIFICATION_REQUIRED: WatchStatus.NEEDS_VERIFICATION,
            ErrorCode.PRODUCT_UNAVAILABLE: WatchStatus.OUT_OF_STOCK,
        }.get(error.code, WatchStatus.ERROR)

    def _on_cancelled(self, task_id: int, context: object) -> None:
        watch_job_id = self._watch_id(context)
        if watch_job_id is None:
            return
        self._in_flight.pop(watch_job_id, None)

    def _on_progress(self, task_id: int, message: str) -> None:
        for watch_job_id, known in self._in_flight.items():
            if known == task_id:
                self.progress.emit(watch_job_id, message)
                return

    @staticmethod
    def _watch_id(context: object) -> int | None:
        if isinstance(context, dict) and context.get("kind") == "watch":
            value = context.get("watch_job_id")
            return int(value) if isinstance(value, int) else None
        return None

    # ---- watch management ------------------------------------------------

    def create_watch(
        self,
        *,
        product_id: int,
        rules: object,
        action: WatchAction,
        interval_seconds: int,
        trigger_in_stock: bool = True,
        trigger_target_price: bool = True,
        expires_at: str | None = None,
    ) -> int:
        """Add a watch job, owning its own copy of the rules."""
        from app.purchasing.models import PurchaseRules

        assert isinstance(rules, PurchaseRules)
        stored = self._rules_repo.create(rules)
        assert stored.rules_id is not None
        job = self._watches.create(
            product_id=product_id,
            rules_id=stored.rules_id,
            action=action,
            interval_seconds=interval_seconds,
            trigger_in_stock=trigger_in_stock,
            trigger_target_price=trigger_target_price,
            expires_at=expires_at,
        )
        product = self._products.get(product_id)
        self._activity.add(
            category=ActivityCategory.SYSTEM,
            severity=ActivitySeverity.INFO,
            title=f"Started watching {product.display_title if product else 'a product'}",
            detail=(
                "Will buy automatically when your rules are met"
                if action is WatchAction.BUY
                else "Will notify you when your rules are met"
            ),
            product_id=product_id,
            watch_job_id=job.id,
        )
        self.watchlist_changed.emit()
        if not self.is_monitoring:
            self.start()
        return job.id

    def pause_watch(self, watch_job_id: int) -> None:
        self._watches.pause(watch_job_id)
        task_id = self._in_flight.pop(watch_job_id, None)
        if task_id is not None:
            self._worker.cancel(task_id)
        self.watchlist_changed.emit()

    def resume_watch(self, watch_job_id: int) -> None:
        self._watches.resume(watch_job_id)
        self.watchlist_changed.emit()
        if not self.is_monitoring:
            self.start()

    def remove_watch(self, watch_job_id: int) -> None:
        task_id = self._in_flight.pop(watch_job_id, None)
        if task_id is not None:
            self._worker.cancel(task_id)
        self._watches.delete(watch_job_id)
        self.watchlist_changed.emit()

    def counts(self) -> dict[str, int]:
        return self._watches.counts()
