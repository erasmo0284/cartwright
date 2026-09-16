"""Driving a purchase through the state machine.

This service is the only thing that moves a purchase forward, and it does so
in exactly one sequence regardless of mode:

    inspect -> validate -> isolate -> checkout -> validate again -> stop

What happens at the final "stop" is the only difference between the modes:

* **Test mode** reports what it found and ends. It never submits, and the
  barrier that guarantees that lives in the checkout manager, not here.
* **Assisted** stops at ``AWAITING_CONFIRMATION`` and waits for a deliberate
  human action.
* **Automatic** continues, but only through the same
  :class:`~app.automation.checkout_manager.SubmitAuthorization` the assisted
  path uses, built from the same guard report. There is no second, weaker
  code path for automatic purchases -- that is the single most important
  structural property of this module.

Every state change is persisted as it happens, so a crash is recoverable, and
an outcome that cannot be read becomes ``UNKNOWN`` rather than a guess.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from PySide6.QtCore import QObject, Signal

from app.automation.amazon_adapter import ADAPTER, PreparedPurchase
from app.automation.browser_worker import BrowserSession, BrowserWorker, Priority
from app.automation.cart_manager import MANAGER as CART
from app.automation.cart_manager import IsolationJournal, IsolationPlan
from app.automation.checkout_manager import SubmitAuthorization
from app.config import SettingsService
from app.core.errors import AppError, ErrorCode
from app.core.money import Money
from app.database.records import (
    ActivityCategory,
    ActivitySeverity,
    ConfirmationStatus,
    OrderRecord,
    ProductRecord,
)
from app.database.repositories import (
    ActivityRepository,
    OrderRepository,
    ProductRepository,
    PurchaseRepository,
    RulesRepository,
    WatchRepository,
)
from app.purchasing.models import (
    CartStrategy,
    CheckoutSnapshot,
    ProductSnapshot,
    PurchaseMode,
    PurchaseRules,
    snapshot_to_metadata,
)
from app.purchasing.purchase_guard import GUARD, verify_total_unchanged
from app.purchasing.states import PurchaseState
from app.purchasing.validation import GuardPhase, GuardReport

logger = logging.getLogger("app.purchasing.service")


@dataclass(frozen=True)
class PurchaseReview:
    """Everything the confirmation screen shows."""

    purchase_job_id: int
    product: ProductRecord
    snapshot: ProductSnapshot
    checkout: CheckoutSnapshot
    rules: PurchaseRules
    report: GuardReport
    strategy: CartStrategy

    @property
    def order_total(self) -> Money | None:
        return self.checkout.order_total

    @property
    def can_confirm(self) -> bool:
        return self.report.passed and self.checkout.order_total is not None


@dataclass(frozen=True)
class TestRunResult:
    """The outcome of a test-mode run. No order was submitted."""

    purchase_job_id: int
    product: ProductRecord
    snapshot: ProductSnapshot
    checkout: CheckoutSnapshot
    report: GuardReport
    strategy: CartStrategy
    order_button_located: bool

    @property
    def succeeded(self) -> bool:
        return self.report.passed and self.order_button_located

    @property
    def headline(self) -> str:
        return "Test successful" if self.succeeded else "Test found a problem"


@dataclass(frozen=True)
class BlockedOutcome:
    """A purchase the guard refused."""

    purchase_job_id: int
    product: ProductRecord
    report: GuardReport | None
    error: AppError

    @property
    def title(self) -> str:
        return "Purchase blocked"


class PurchaseService(QObject):
    """Runs purchases. The only caller of the submit path."""

    progress = Signal(int, str)
    state_changed = Signal(int, str)
    #: A :class:`PurchaseReview`; assisted mode waits for the user.
    ready_for_confirmation = Signal(object)
    #: A :class:`TestRunResult`.
    test_completed = Signal(object)
    #: An :class:`OrderRecord`.
    purchase_completed = Signal(object)
    #: A :class:`BlockedOutcome`.
    purchase_blocked = Signal(object)
    #: purchase_job_id, AppError
    purchase_failed = Signal(int, object)
    #: purchase_job_id -- the outcome could not be established.
    purchase_uncertain = Signal(int)
    purchase_cancelled = Signal(int)

    def __init__(
        self,
        *,
        worker: BrowserWorker,
        products: ProductRepository,
        rules_repo: RulesRepository,
        purchases: PurchaseRepository,
        orders: OrderRepository,
        watches: WatchRepository,
        activity: ActivityRepository,
        settings: SettingsService,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._worker = worker
        self._products = products
        self._rules_repo = rules_repo
        self._purchases = purchases
        self._orders = orders
        self._watches = watches
        self._activity = activity
        self._settings = settings
        #: Reviews awaiting a human decision, keyed by purchase job id.
        self._pending: dict[int, PurchaseReview] = {}
        self._tasks: dict[int, int] = {}

        worker.progress.connect(self._on_progress)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_cancelled)

    # ---- starting --------------------------------------------------------

    def start(
        self,
        *,
        product_id: int,
        rules: PurchaseRules,
        mode: PurchaseMode,
        allow_set_aside: bool = False,
        watch_job_id: int | None = None,
        test_mode: bool | None = None,
    ) -> int:
        """Begin a purchase. Returns the purchase job id.

        Raises :class:`AppError` synchronously when the product already has a
        live purchase -- including one with an unresolved outcome -- so a
        duplicate can never even be queued.
        """
        effective_test_mode = (
            self._settings.current.test_mode if test_mode is None else test_mode
        )

        if mode is PurchaseMode.AUTOMATIC and not self._settings.current.auto_buy_acknowledged:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                context={"reason": "auto_buy_not_acknowledged"},
                detail_override=(
                    "Automatic purchasing has not been switched on for this "
                    "app yet. Turn it on in Settings first."
                ),
            )

        product = self._products.get(product_id)
        if product is None:
            raise AppError(
                ErrorCode.PRODUCT_NOT_FOUND, context={"product_id": product_id}
            )

        stored_rules = self._rules_repo.create(
            replace(rules, brand=rules.brand or product.brand)
        )
        assert stored_rules.rules_id is not None

        job = self._purchases.create(
            product_id=product_id,
            rules_id=stored_rules.rules_id,
            mode=mode,
            test_mode=effective_test_mode,
            watch_job_id=watch_job_id,
        )

        label = (
            f"Test run for {product.display_title}"
            if effective_test_mode
            else f"Purchase {product.display_title}"
        )
        task_id = self._worker.submit(
            label,
            lambda session: self._run_preparation(
                session,
                job_id=job.id,
                product=product,
                rules=replace(stored_rules, brand=stored_rules.brand or product.brand),
                mode=mode,
                test_mode=effective_test_mode,
                allow_set_aside=allow_set_aside,
            ),
            priority=Priority.PURCHASE,
            context={"kind": "purchase", "job_id": job.id},
        )
        self._tasks[job.id] = task_id
        return job.id

    def cancel(self, purchase_job_id: int) -> None:
        """Stop a purchase that has not been submitted."""
        job = self._purchases.get(purchase_job_id)
        if job is not None and job.state.order_may_exist:
            logger.warning(
                "Refusing to cancel a purchase that may already be an order",
                extra={"purchase_job_id": purchase_job_id, "state": job.state.value},
            )
            return
        task_id = self._tasks.get(purchase_job_id)
        if task_id is not None:
            self._worker.cancel(task_id)
        self._pending.pop(purchase_job_id, None)

    # ---- the preparation sequence ---------------------------------------

    def _run_preparation(
        self,
        session: BrowserSession,
        *,
        job_id: int,
        product: ProductRecord,
        rules: PurchaseRules,
        mode: PurchaseMode,
        test_mode: bool,
        allow_set_aside: bool,
    ) -> None:
        journal = IsolationJournal()
        try:
            self._transition(job_id, PurchaseState.PRODUCT_CHECK)
            snapshot = ADAPTER.inspect_product(
                session, asin=rules.expected_asin, marketplace=product.marketplace
            )
            self._products.upsert_from_snapshot(snapshot)
            self._products.record_observation(product.id, snapshot)

            self._transition(job_id, PurchaseState.RULE_VALIDATION)
            session.step("Checking your rules...")
            product_report = GUARD.check_product(rules, snapshot)
            self._purchases.save_guard_report(job_id, product_report)
            if not product_report.passed:
                self._block(job_id, product, product_report)
                return

            session.step("Checking your Amazon cart...")
            cart = ADAPTER.read_cart(session)
            plan = CART.plan_isolation(
                product=snapshot,
                rules=rules,
                cart=cart,
                allow_set_aside=allow_set_aside,
            )
            if plan.is_blocked:
                # The unrelated item titles travel with the error so the UI can
                # list them and offer to set them aside, rather than making the
                # user go and look.
                self._block_with_error(
                    job_id,
                    product,
                    AppError(
                        ErrorCode.CART_CONFLICT,
                        context={
                            "cart_lines": len(cart.lines),
                            "unexpected": [
                                line.display_title for line in plan.foreign_lines[:10]
                            ],
                            "can_set_aside": bool(
                                plan.foreign_lines and snapshot.add_to_cart_available
                            ),
                        },
                        detail_override=plan.reason,
                    ),
                )
                return
            self._purchases.set_cart_strategy(job_id, plan.strategy)

            self._transition(job_id, PurchaseState.CART_PREPARATION)
            prepared = ADAPTER.prepare_purchase(
                session,
                rules=rules,
                plan=plan,
                marketplace=product.marketplace,
                journal=journal,
                on_journal_change=lambda current: self._purchases.set_isolation_journal(
                    job_id, current.to_json()
                ),
            )
            journal = prepared.journal

            self._transition(job_id, PurchaseState.CHECKOUT)
            self._transition(job_id, PurchaseState.FINAL_VALIDATION)
            session.step("Running the final checks...")
            final_report = GUARD.check_final(rules, prepared.product, prepared.checkout)
            self._purchases.save_guard_report(job_id, final_report)

            if test_mode:
                self._finish_test_run(
                    session, job_id, product, rules, prepared, final_report, journal
                )
                return

            if not final_report.passed:
                ADAPTER.restore_cart(session, journal)
                self._block(job_id, product, final_report)
                return

            review = PurchaseReview(
                purchase_job_id=job_id,
                product=product,
                snapshot=prepared.product,
                checkout=prepared.checkout,
                rules=rules,
                report=final_report,
                strategy=prepared.strategy,
            )

            if mode is PurchaseMode.ASSISTED:
                self._pending[job_id] = review
                self._transition(job_id, PurchaseState.AWAITING_CONFIRMATION)
                self._activity.add(
                    category=ActivityCategory.PURCHASE,
                    severity=ActivitySeverity.INFO,
                    title=f"Ready to purchase {product.display_title}",
                    detail=(
                        f"Total {prepared.checkout.order_total.format()}"
                        if prepared.checkout.order_total
                        else "Waiting for your confirmation"
                    ),
                    product_id=product.id,
                    purchase_job_id=job_id,
                    amount=prepared.checkout.order_total,
                )
                self.ready_for_confirmation.emit(review)
                return

            # Automatic mode: continue immediately, through the same
            # authorisation the assisted path builds.
            self._submit(session, review, journal)

        except AppError as error:
            self._handle_preparation_error(session, job_id, product, error, journal)
            raise
        except Exception:
            # The worker wraps and reports this; the job is failed here so it
            # does not linger in a live state.
            self._fail(job_id, AppError(ErrorCode.INTERNAL_ERROR))
            self._safe_restore(session, journal)
            raise

    def _finish_test_run(
        self,
        session: BrowserSession,
        job_id: int,
        product: ProductRecord,
        rules: PurchaseRules,
        prepared: PreparedPurchase,
        report: GuardReport,
        journal: IsolationJournal,
    ) -> None:
        """End a test run without submitting, and put the cart back."""
        result = TestRunResult(
            purchase_job_id=job_id,
            product=product,
            snapshot=prepared.product,
            checkout=prepared.checkout,
            report=report,
            strategy=prepared.strategy,
            order_button_located=prepared.checkout.place_order_control_found,
        )
        ADAPTER.restore_cart(session, journal)
        self._purchases.transition(
            job_id,
            PurchaseState.CANCELLED,
            reason="test mode: stopped before ordering",
            outcome_code="test_mode",
            outcome_detail=(
                "Test completed. The final order button was located but NOT "
                "clicked, and no order was submitted."
            ),
        )
        self.state_changed.emit(job_id, PurchaseState.CANCELLED.value)
        self._activity.add(
            category=ActivityCategory.PURCHASE,
            severity=(
                ActivitySeverity.SUCCESS if result.succeeded else ActivitySeverity.WARNING
            ),
            title=f"{result.headline}: {product.display_title}",
            detail=(
                "No order was submitted. " + report.summary
            ),
            product_id=product.id,
            purchase_job_id=job_id,
            amount=prepared.checkout.order_total,
            metadata=snapshot_to_metadata(prepared.product),
        )
        logger.info(
            "Test run finished",
            extra={
                "purchase_job_id": job_id,
                "passed": report.passed,
                "order_button_located": result.order_button_located,
            },
        )
        self.test_completed.emit(result)

    # ---- confirmation and submission ------------------------------------

    def confirm(self, purchase_job_id: int) -> int | None:
        """Place an order the user has just approved.

        Called from a deliberate action on the confirmation screen. The
        review must still be pending: a confirmation for a job that already
        moved on is refused rather than replayed.
        """
        review = self._pending.pop(purchase_job_id, None)
        if review is None:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                context={"reason": "no_pending_review", "job": purchase_job_id},
                detail_override=(
                    "This purchase is no longer waiting for confirmation. "
                    "Start it again if you still want it."
                ),
            )
        job = self._purchases.get(purchase_job_id)
        if job is None or job.state is not PurchaseState.AWAITING_CONFIRMATION:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                context={
                    "reason": "not_awaiting_confirmation",
                    "state": job.state.value if job else None,
                },
                detail_override="This purchase is not ready to be placed.",
            )
        if self._purchases.has_submitted(purchase_job_id):
            raise AppError(
                ErrorCode.DUPLICATE_BLOCKED,
                context={"purchase_job_id": purchase_job_id},
            )

        # The record already parsed the stored JSON into a mapping, so it is
        # rebuilt from that rather than re-serialised.
        journal = IsolationJournal.from_mapping(job.isolation_journal)

        task_id = self._worker.submit(
            f"Place order for {review.product.display_title}",
            lambda session: self._submit(session, review, journal),
            priority=Priority.PURCHASE,
            context={"kind": "purchase", "job_id": purchase_job_id},
        )
        self._tasks[purchase_job_id] = task_id
        return task_id

    def _submit(
        self,
        session: BrowserSession,
        review: PurchaseReview,
        journal: IsolationJournal,
    ) -> None:
        """Submit the order and establish what happened."""
        job_id = review.purchase_job_id
        rules = review.rules

        session.step("Re-checking the order before placing it...")
        current = ADAPTER.refresh_checkout(session)

        unchanged = verify_total_unchanged(review.checkout.order_total, current.order_total)
        recheck = GUARD.check_final(rules, review.snapshot, current).with_checks([unchanged])
        self._purchases.save_guard_report(job_id, recheck)
        if not recheck.passed:
            ADAPTER.restore_cart(session, journal)
            self._block(job_id, review.product, recheck)
            return

        approved_total = current.order_total
        if approved_total is None:  # pragma: no cover - guarded above
            ADAPTER.restore_cart(session, journal)
            self._block(job_id, review.product, recheck)
            return

        attempt = self._purchases.begin_attempt(job_id)
        authorization = SubmitAuthorization(
            purchase_job_id=job_id,
            attempt_id=attempt.id,
            approved_total=approved_total,
            guard_passed=True,
            test_mode=False,
            record_submission=lambda: self._purchases.mark_submitted(attempt),
        )

        self._transition(job_id, PurchaseState.SUBMITTING)
        try:
            confirmation = ADAPTER.submit_order(session, authorization)
        except AppError as error:
            # Whether an order exists depends on how far the click got. If the
            # submission was recorded, an order may exist and the outcome is
            # uncertain; otherwise nothing was ordered.
            if self._purchases.has_submitted(job_id):
                self._purchases.finish_attempt(
                    attempt, outcome="uncertain", detail=error.detail
                )
                self._mark_uncertain(job_id, review.product, error.detail)
            else:
                self._purchases.finish_attempt(
                    attempt, outcome="not_submitted", detail=error.detail
                )
                self._transition(
                    job_id,
                    PurchaseState.FAILED,
                    reason=error.code.value,
                    outcome_code=error.code.value,
                    outcome_detail=error.detail,
                )
                self._activity.add(
                    category=ActivityCategory.PURCHASE,
                    severity=ActivitySeverity.ERROR,
                    title=f"Order not placed: {review.product.display_title}",
                    detail=error.detail,
                    product_id=review.product.id,
                    purchase_job_id=job_id,
                    error_code=error.code.value,
                )
                self.purchase_failed.emit(job_id, error)
            self._safe_restore(session, journal)
            return

        self._transition(job_id, PurchaseState.CONFIRMING)

        if not confirmation.verified:
            self._purchases.finish_attempt(
                attempt,
                outcome="uncertain",
                detail="Amazon's confirmation could not be read",
            )
            self._mark_uncertain(job_id, review.product, None)
            self._safe_restore(session, journal)
            return

        order = self._orders.record(
            purchase_job_id=job_id,
            product_id=review.product.id,
            rules=rules,
            checkout=current,
            confirmation=confirmation,
            seller=review.snapshot.seller,
            condition=review.snapshot.condition,
            status=ConfirmationStatus.CONFIRMED,
        )
        self._purchases.finish_attempt(
            attempt, outcome="confirmed", detail=confirmation.order_number
        )
        self._transition(
            job_id,
            PurchaseState.CONFIRMED,
            reason="confirmed by Amazon",
            outcome_detail=f"Order {confirmation.order_number or 'placed'}",
        )

        job = self._purchases.get(job_id)
        if job is not None and job.watch_job_id is not None:
            # Stops the watch permanently, which is what prevents an auto-buy
            # watch from ordering the same item again.
            self._watches.mark_purchase_completed(job.watch_job_id)

        self._activity.add(
            category=ActivityCategory.PURCHASE,
            severity=ActivitySeverity.SUCCESS,
            title=f"Purchased {review.product.display_title}",
            detail=(
                f"Order {confirmation.order_number}"
                if confirmation.order_number
                else "Order confirmed by Amazon"
            ),
            product_id=review.product.id,
            purchase_job_id=job_id,
            order_id=order.id,
            amount=order.total,
        )
        logger.warning(
            "Purchase confirmed",
            extra={
                "purchase_job_id": job_id,
                "order_number": confirmation.order_number,
                "total_cents": order.total.cents,
            },
        )
        self._safe_restore(session, journal)
        self.purchase_completed.emit(order)

    # ---- uncertain outcomes ---------------------------------------------

    def _mark_uncertain(
        self, job_id: int, product: ProductRecord, detail: str | None
    ) -> None:
        """Record that an order may exist, and stop.

        No retry, ever. The only way out is :meth:`resolve_uncertain`, driven
        by what the user finds in their Amazon orders.
        """
        self._purchases.transition(
            job_id,
            PurchaseState.UNKNOWN,
            reason="the order result could not be read",
            outcome_code=ErrorCode.ORDER_RESULT_UNCERTAIN.value,
            outcome_detail=detail
            or (
                "The order was submitted but Amazon's confirmation could not "
                "be read. The app will NOT try again."
            ),
        )
        self.state_changed.emit(job_id, PurchaseState.UNKNOWN.value)
        self._activity.add(
            category=ActivityCategory.PURCHASE,
            severity=ActivitySeverity.ERROR,
            title=f"Order result uncertain: {product.display_title}",
            detail=(
                "Check your Amazon orders, then tell the app whether the order "
                "was placed."
            ),
            product_id=product.id,
            purchase_job_id=job_id,
            error_code=ErrorCode.ORDER_RESULT_UNCERTAIN.value,
        )
        logger.error(
            "Purchase outcome uncertain", extra={"purchase_job_id": job_id}
        )
        self.purchase_uncertain.emit(job_id)

    def resolve_uncertain(
        self, purchase_job_id: int, *, order_was_placed: bool, note: str
    ) -> OrderRecord | None:
        """Close an uncertain purchase using the user's own answer."""
        job = self._purchases.get(purchase_job_id)
        if job is None:
            raise AppError(
                ErrorCode.INTERNAL_ERROR, context={"job": purchase_job_id}
            )
        product = (
            self._products.get(job.product_id) if job.product_id else None
        )
        self._purchases.resolve_unknown(
            purchase_job_id, order_was_placed=order_was_placed, note=note
        )

        order: OrderRecord | None = None
        if order_was_placed:
            report = self._purchases.latest_guard_report(
                purchase_job_id, GuardPhase.PRE_SUBMIT
            )
            total_cents = None
            if report:
                for check in report["checks"]:
                    if check["check_id"] == "order_total" and check["actual"]:
                        from app.core.money import parse_money

                        parsed = parse_money(str(check["actual"]))
                        total_cents = parsed.cents if parsed else None
                        break
            rules = self._rules_repo.get(job.rules_id)
            order = self._orders.record_user_confirmed(
                purchase_job_id=purchase_job_id,
                product_id=job.product_id,
                total=Money(total_cents or 0),
                quantity=rules.quantity if rules else 1,
                order_number=None,
                note=note,
            )
            if job.watch_job_id is not None:
                self._watches.mark_purchase_completed(job.watch_job_id)

        self._activity.add(
            category=ActivityCategory.PURCHASE,
            severity=(
                ActivitySeverity.SUCCESS if order_was_placed else ActivitySeverity.INFO
            ),
            title=(
                f"You confirmed the order was placed"
                if order_was_placed
                else "You confirmed no order was placed"
            ),
            detail=note,
            product_id=job.product_id,
            purchase_job_id=purchase_job_id,
            order_id=order.id if order else None,
        )
        return order

    def verify_uncertain(self, purchase_job_id: int) -> int | None:
        """Look the order up on Amazon to help the user resolve it.

        Read-only. The user still makes the call; this just saves them
        hunting through their order history.
        """
        job = self._purchases.get(purchase_job_id)
        if job is None or job.state is not PurchaseState.UNKNOWN:
            return None
        return self._worker.submit(
            "Check your Amazon orders",
            lambda session: ADAPTER.verify_order(session),
            priority=Priority.INTERACTIVE,
            context={"kind": "verify", "job_id": purchase_job_id},
        )

    # ---- outcome helpers -------------------------------------------------

    def _transition(
        self,
        job_id: int,
        state: PurchaseState,
        *,
        reason: str | None = None,
        outcome_code: str | None = None,
        outcome_detail: str | None = None,
    ) -> None:
        self._purchases.transition(
            job_id,
            state,
            reason=reason,
            outcome_code=outcome_code,
            outcome_detail=outcome_detail,
        )
        self.state_changed.emit(job_id, state.value)

    def _block(
        self, job_id: int, product: ProductRecord, report: GuardReport
    ) -> None:
        failures = report.blocking_failures
        first = failures[0] if failures else None
        code = report.blocked_code or ErrorCode.INTERNAL_ERROR
        detail = (
            f"{first.title}: expected {first.expected}, found {first.actual}"
            if first and first.expected and first.actual
            else (first.title if first else "A required check did not pass")
        )
        self._purchases.transition(
            job_id,
            PurchaseState.BLOCKED,
            reason=code.value,
            outcome_code=code.value,
            outcome_detail=detail,
        )
        self.state_changed.emit(job_id, PurchaseState.BLOCKED.value)
        self._activity.add(
            category=ActivityCategory.PURCHASE,
            severity=ActivitySeverity.BLOCKED,
            title=f"Purchase blocked: {product.display_title}",
            detail=detail,
            product_id=product.id,
            purchase_job_id=job_id,
            error_code=code.value,
        )
        logger.warning(
            "Purchase blocked",
            extra={"purchase_job_id": job_id, "code": code.value, "detail": detail},
        )
        self.purchase_blocked.emit(
            BlockedOutcome(
                purchase_job_id=job_id,
                product=product,
                report=report,
                error=AppError(code, detail_override=detail),
            )
        )

    def _block_with_error(
        self, job_id: int, product: ProductRecord, error: AppError
    ) -> None:
        self._purchases.transition(
            job_id,
            PurchaseState.BLOCKED,
            reason=error.code.value,
            outcome_code=error.code.value,
            outcome_detail=error.detail,
        )
        self.state_changed.emit(job_id, PurchaseState.BLOCKED.value)
        self._activity.add(
            category=ActivityCategory.PURCHASE,
            severity=ActivitySeverity.BLOCKED,
            title=f"Purchase blocked: {product.display_title}",
            detail=error.detail,
            product_id=product.id,
            purchase_job_id=job_id,
            error_code=error.code.value,
        )
        self.purchase_blocked.emit(
            BlockedOutcome(
                purchase_job_id=job_id, product=product, report=None, error=error
            )
        )

    def _fail(self, job_id: int, error: AppError) -> None:
        job = self._purchases.get(job_id)
        if job is None or job.state.is_terminal:
            return
        if job.state.order_may_exist:
            self._mark_uncertain(job_id, self._products.get(job.product_id), error.detail)
            return
        self._purchases.transition(
            job_id,
            PurchaseState.FAILED,
            reason=error.code.value,
            outcome_code=error.code.value,
            outcome_detail=error.detail,
        )
        self.state_changed.emit(job_id, PurchaseState.FAILED.value)
        self.purchase_failed.emit(job_id, error)

    def _handle_preparation_error(
        self,
        session: BrowserSession,
        job_id: int,
        product: ProductRecord,
        error: AppError,
        journal: IsolationJournal,
    ) -> None:
        """Record a failure during preparation, and undo any cart changes."""
        job = self._purchases.get(job_id)
        if job is not None and job.state.order_may_exist:
            self._mark_uncertain(job_id, product, error.detail)
        elif error.needs_user:
            self._purchases.transition(
                job_id,
                PurchaseState.NEEDS_USER,
                reason=error.code.value,
                outcome_code=error.code.value,
                outcome_detail=error.detail,
            )
            self.state_changed.emit(job_id, PurchaseState.NEEDS_USER.value)
        else:
            self._purchases.transition(
                job_id,
                PurchaseState.FAILED,
                reason=error.code.value,
                outcome_code=error.code.value,
                outcome_detail=error.detail,
            )
            self.state_changed.emit(job_id, PurchaseState.FAILED.value)

        self._activity.add(
            category=ActivityCategory.ERROR if not error.needs_user else ActivityCategory.WARNING,
            severity=ActivitySeverity.ERROR if not error.needs_user else ActivitySeverity.WARNING,
            title=f"Purchase stopped: {product.display_title}",
            detail=error.detail,
            product_id=product.id,
            purchase_job_id=job_id,
            error_code=error.code.value,
        )
        self._safe_restore(session, journal)

    def _safe_restore(
        self, session: BrowserSession, journal: IsolationJournal
    ) -> None:
        """Put the user's cart back, never letting that failure mask another."""
        if not journal.has_pending_restore:
            return
        try:
            ADAPTER.restore_cart(session, journal)
        except Exception:  # noqa: BLE001
            logger.warning("Could not restore the cart", exc_info=True)

    # ---- recovery --------------------------------------------------------

    def recover_after_restart(self) -> list[int]:
        """Resolve purchases interrupted by a crash. Called once at startup."""
        uncertain = self._purchases.recover_interrupted()
        for job_id in uncertain:
            job = self._purchases.get(job_id)
            product = self._products.get(job.product_id) if job else None
            self._activity.add(
                category=ActivityCategory.PURCHASE,
                severity=ActivitySeverity.ERROR,
                title="A purchase was interrupted",
                detail=(
                    "The app stopped while an order was being placed, so it "
                    "cannot tell whether Amazon accepted it. Check your Amazon "
                    "orders. The app will NOT try again."
                ),
                product_id=product.id if product else None,
                purchase_job_id=job_id,
                error_code=ErrorCode.ORDER_RESULT_UNCERTAIN.value,
            )
            self.purchase_uncertain.emit(job_id)
        return uncertain

    def pending_review(self, purchase_job_id: int) -> PurchaseReview | None:
        return self._pending.get(purchase_job_id)

    # ---- worker signal plumbing -----------------------------------------

    @staticmethod
    def _is_ours(context: object) -> int | None:
        if isinstance(context, dict) and context.get("kind") == "purchase":
            value = context.get("job_id")
            return int(value) if isinstance(value, int) else None
        return None

    def _on_progress(self, task_id: int, message: str) -> None:
        for job_id, known in self._tasks.items():
            if known == task_id:
                self.progress.emit(job_id, message)
                return

    def _on_failed(self, task_id: int, error: object, context: object) -> None:
        job_id = self._is_ours(context)
        if job_id is None or not isinstance(error, AppError):
            return
        self._tasks.pop(job_id, None)
        self._pending.pop(job_id, None)
        self._fail(job_id, error)

    def _on_cancelled(self, task_id: int, context: object) -> None:
        job_id = self._is_ours(context)
        if job_id is None:
            return
        self._tasks.pop(job_id, None)
        self._pending.pop(job_id, None)
        job = self._purchases.get(job_id)
        if job is None or job.state.is_terminal:
            return
        if job.state.order_may_exist:
            self._mark_uncertain(job_id, self._products.get(job.product_id), None)
            return
        self._purchases.transition(
            job_id,
            PurchaseState.CANCELLED,
            reason="stopped by the user",
            outcome_detail="You stopped this purchase. Nothing was ordered.",
        )
        self.state_changed.emit(job_id, PurchaseState.CANCELLED.value)
        self.purchase_cancelled.emit(job_id)
