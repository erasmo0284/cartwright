"""Tests for the orchestration layer.

The guard, the state machine and the repositories are tested in isolation
elsewhere. What is tested here is the *wiring*: that the purchase service
drives the state machine in the right order, that test mode really stops at
the guard, that automatic mode goes through the same authorisation, that an
unreadable outcome becomes ``UNKNOWN`` and is never retried, and that the
monitor service hands a triggered watch to the purchase service rather than
deciding anything itself.

The browser is replaced by a fake worker that runs each task immediately on
the calling thread. That makes these tests deterministic and fast, and it is
honest about what they cover: the sequencing and the persistence, not
Playwright.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, replace
from typing import Any, Callable

import pytest
from PySide6.QtCore import QObject, Signal

from app.automation.cart_manager import IsolationPlan
from app.automation.checkout_manager import SubmitAuthorization
from app.config import SettingsService
from app.core.errors import AppError, ErrorCode
from app.core.money import Money
from app.database.database import Database
from app.database.records import WatchAction, WatchStatus
from app.database.repositories import (
    ActivityRepository,
    OrderRepository,
    ProductRepository,
    PurchaseRepository,
    RulesRepository,
    WatchRepository,
)
from app.purchasing.models import (
    Availability,
    CartLine,
    CartState,
    CartStrategy,
    CheckoutSnapshot,
    ConditionPolicy,
    ItemCondition,
    OrderConfirmation,
    ProductSnapshot,
    PurchaseMode,
    PurchaseRules,
    SellerPolicy,
    VariationSnapshot,
)
from app.purchasing.purchase_service import PurchaseService
from app.purchasing.states import PurchaseState
from app.purchasing.validation import GuardPhase

ASIN = "B01N5OSTVQ"


def usd(amount: str) -> Money:
    return Money.from_decimal(amount, "USD")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSession:
    """Stands in for :class:`BrowserSession`; records the progress steps."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.shown = 0

    def step(self, message: str) -> None:
        self.steps.append(message)

    def check_cancelled(self) -> None:
        return None

    def show_browser(self) -> None:
        self.shown += 1

    def sleep(self, seconds: float) -> None:
        return None

    @property
    def manager(self) -> Any:
        raise AssertionError("a service must not reach the browser manager")


class FakeWorker(QObject):
    """A browser worker that runs tasks immediately, in order of priority."""

    progress = Signal(int, str)
    finished = Signal(int, object, object)
    failed = Signal(int, object, object)
    cancelled = Signal(int, object)
    state_changed = Signal(str)
    install_progress = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._ids = itertools.count(1)
        self.submitted: list[tuple[str, Any]] = []
        self.session = FakeSession()
        #: When set, the next submitted task is not run but recorded.
        self.defer = False
        self.deferred: list[tuple[int, Callable[[Any], Any], Any]] = []

    def submit(
        self,
        label: str,
        run: Callable[[Any], Any],
        *,
        priority: Any = None,
        context: Any = None,
    ) -> int:
        task_id = next(self._ids)
        self.submitted.append((label, context))
        if self.defer:
            self.deferred.append((task_id, run, context))
            return task_id
        self._execute(task_id, run, context)
        return task_id

    def run_deferred(self) -> None:
        pending, self.deferred = self.deferred, []
        for task_id, run, context in pending:
            self._execute(task_id, run, context)

    def _execute(self, task_id: int, run: Callable[[Any], Any], context: Any) -> None:
        try:
            result = run(self.session)
        except AppError as error:
            self.failed.emit(task_id, error, context)
            return
        except Exception as exc:  # noqa: BLE001 - mirrors the real worker
            self.failed.emit(
                task_id,
                AppError(ErrorCode.INTERNAL_ERROR, context={"exception": str(exc)}),
                context,
            )
            return
        self.finished.emit(task_id, result, context)

    def cancel(self, task_id: int) -> bool:
        self.cancelled.emit(task_id, None)
        return True

    def cancel_all(self, *, only_priority: Any = None) -> int:
        return 0


@dataclass
class FakeAdapter:
    """Scripted stand-in for :data:`app.automation.amazon_adapter.ADAPTER`."""

    snapshot: ProductSnapshot
    checkout: CheckoutSnapshot
    cart: CartState
    confirmation: OrderConfirmation
    submit_error: AppError | None = None
    inspect_error: AppError | None = None
    submitted: list[SubmitAuthorization] = None  # type: ignore[assignment]
    restored: int = 0
    #: What the cart holds after the item was added, on the cart route. The
    #: real adapter re-reads the cart there and hands it to ``on_cart_read``.
    prepared_cart: CartState | None = None
    #: How many times the pre-checkout callback was invoked.
    cart_guard_calls: int = 0

    def __post_init__(self) -> None:
        if self.submitted is None:
            self.submitted = []

    def inspect_product(self, session, *, asin, marketplace="www.amazon.com"):
        if self.inspect_error is not None:
            raise self.inspect_error
        return self.snapshot

    def read_cart(self, session):
        return self.cart

    def prepare_purchase(self, session, *, rules, plan, marketplace="www.amazon.com",
                         journal=None, on_journal_change=None, on_cart_read=None):
        from app.automation.amazon_adapter import PreparedPurchase
        from app.automation.cart_manager import IsolationJournal

        if plan.strategy is not CartStrategy.BUY_NOW and callable(on_cart_read):
            # Mirrors the real adapter: the cart is re-read after the item was
            # added, and the caller gets to refuse before the checkout opens.
            cart = self.prepared_cart
            if cart is None:
                cart = CartState(
                    lines=(
                        CartLine(
                            asin=self.snapshot.asin,
                            title=self.snapshot.title,
                            quantity=rules.quantity,
                            unit_price=self.snapshot.price,
                        ),
                    ),
                    subtotal=self.snapshot.price,
                )
            self.cart_guard_calls += 1
            on_cart_read(self.snapshot, cart)

        return PreparedPurchase(
            product=self.snapshot,
            checkout=self.checkout,
            strategy=plan.strategy,
            journal=journal or IsolationJournal(),
        )

    def refresh_checkout(self, session):
        return self.checkout

    def submit_order(self, session, authorization):
        self.submitted.append(authorization)
        # The real manager records the submission before clicking; a fake that
        # skipped it would let the "never retried" tests pass for the wrong
        # reason.
        authorization.record_submission()
        if self.submit_error is not None:
            raise self.submit_error
        return self.confirmation

    def restore_cart(self, session, journal):
        self.restored += 1
        return 0

    def verify_order(self, session, *, expected_total=None):
        return self.confirmation


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def snapshot() -> ProductSnapshot:
    return ProductSnapshot(
        asin=ASIN,
        title="Klein Tools CL800 Clamp Meter",
        brand="Klein Tools",
        price=usd("109.97"),
        availability=Availability.IN_STOCK,
        availability_text="In Stock",
        seller="Amazon.com",
        ships_from="Amazon.com",
        condition=ItemCondition.NEW,
        variation=VariationSnapshot({"Color": "Black"}),
        max_quantity=30,
        buy_now_available=True,
        add_to_cart_available=True,
    )


@pytest.fixture
def checkout() -> CheckoutSnapshot:
    return CheckoutSnapshot(
        lines=(
            CartLine(asin=ASIN, title="Clamp meter", quantity=1, unit_price=usd("109.97")),
        ),
        item_subtotal=usd("109.97"),
        shipping=usd("0.00"),
        tax=usd("7.97"),
        order_total=usd("117.94"),
        address_label="John D., Raleigh, NC",
        payment_label="Visa ending in 1234",
        place_order_control_found=True,
    )


@pytest.fixture
def rules() -> PurchaseRules:
    return PurchaseRules(
        expected_asin=ASIN,
        quantity=1,
        max_item_price=usd("120.00"),
        max_order_total=usd("135.00"),
        seller_policy=SellerPolicy.AMAZON_ONLY,
        condition_policy=ConditionPolicy.NEW_ONLY,
        expected_variation=VariationSnapshot({"Color": "Black"}),
        expected_seller="Amazon.com",
        expected_address_label="John D., Raleigh, NC",
        expected_payment_label="Visa ending in 1234",
        brand="Klein Tools",
    )


@dataclass
class Harness:
    service: PurchaseService
    worker: FakeWorker
    adapter: FakeAdapter
    settings: SettingsService
    products: ProductRepository
    purchases: PurchaseRepository
    orders: OrderRepository
    watches: WatchRepository
    rules_repo: RulesRepository
    activity: ActivityRepository
    product_id: int
    events: dict[str, list[Any]]


@pytest.fixture
def harness(
    database: Database, monkeypatch, snapshot, checkout
) -> Harness:
    products = ProductRepository(database)
    rules_repo = RulesRepository(database)
    purchases = PurchaseRepository(database)
    orders = OrderRepository(database)
    watches = WatchRepository(database)
    activity = ActivityRepository(database)
    settings = SettingsService(database)

    record = products.upsert_from_snapshot(snapshot)

    adapter = FakeAdapter(
        snapshot=snapshot,
        checkout=checkout,
        cart=CartState(reported_empty=True),
        confirmation=OrderConfirmation(
            verified=True,
            order_number="112-1234567-7654321",
            order_total=usd("117.94"),
        ),
    )
    monkeypatch.setattr("app.purchasing.purchase_service.ADAPTER", adapter)

    worker = FakeWorker()
    service = PurchaseService(
        worker=worker,
        products=products,
        rules_repo=rules_repo,
        purchases=purchases,
        orders=orders,
        watches=watches,
        activity=activity,
        settings=settings,
    )

    events: dict[str, list[Any]] = {
        "ready": [], "test": [], "blocked": [], "completed": [],
        "failed": [], "uncertain": [], "cancelled": [], "progress": [],
    }
    service.ready_for_confirmation.connect(lambda r: events["ready"].append(r))
    service.test_completed.connect(lambda r: events["test"].append(r))
    service.purchase_blocked.connect(lambda r: events["blocked"].append(r))
    service.purchase_completed.connect(lambda r: events["completed"].append(r))
    service.purchase_failed.connect(lambda i, e: events["failed"].append((i, e)))
    service.purchase_uncertain.connect(lambda i: events["uncertain"].append(i))
    service.purchase_cancelled.connect(lambda i: events["cancelled"].append(i))
    service.progress.connect(lambda i, m: events["progress"].append(m))

    return Harness(
        service=service, worker=worker, adapter=adapter, settings=settings,
        products=products, purchases=purchases, orders=orders, watches=watches,
        rules_repo=rules_repo, activity=activity, product_id=record.id,
        events=events,
    )


# ---------------------------------------------------------------------------
# Test mode
# ---------------------------------------------------------------------------


class TestTestMode:
    def test_a_test_run_never_submits(self, harness, rules) -> None:
        harness.settings.update(test_mode=True)
        job_id = harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        assert harness.adapter.submitted == []
        assert not harness.purchases.has_submitted(job_id)
        assert harness.events["test"], "a test result should be reported"
        assert harness.events["ready"] == [], "test mode must not ask for confirmation"

    def test_a_successful_test_run_reports_success(self, harness, rules) -> None:
        harness.settings.update(test_mode=True)
        harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        result = harness.events["test"][0]
        assert result.succeeded
        assert result.order_button_located
        assert result.headline == "Test successful"

    def test_a_test_run_ends_the_job_and_frees_the_product(
        self, harness, rules
    ) -> None:
        harness.settings.update(test_mode=True)
        job_id = harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        job = harness.purchases.get(job_id)
        assert job is not None
        assert job.state.is_terminal
        assert job.outcome_code == "test_mode"
        assert "NOT clicked" in (job.outcome_detail or "")
        # A second run is possible straight away.
        harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.ASSISTED
        )

    def test_a_test_run_restores_the_cart(self, harness, rules) -> None:
        harness.settings.update(test_mode=True)
        harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        assert harness.adapter.restored >= 1

    def test_a_failing_test_run_says_what_failed(self, harness, rules) -> None:
        harness.settings.update(test_mode=True)
        harness.adapter.snapshot = replace(
            harness.adapter.snapshot, seller="XYZ Marketplace LLC"
        )
        harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        # A failing product check blocks before reaching the test report.
        assert harness.events["blocked"]
        assert harness.adapter.submitted == []


# ---------------------------------------------------------------------------
# Assisted purchases
# ---------------------------------------------------------------------------


class TestAssistedPurchase:
    def _prepare(self, harness, rules) -> int:
        harness.settings.update(test_mode=False)
        return harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.ASSISTED
        )

    def test_stops_and_asks(self, harness, rules) -> None:
        job_id = self._prepare(harness, rules)
        assert harness.events["ready"], "the user should be asked"
        assert harness.adapter.submitted == []
        job = harness.purchases.get(job_id)
        assert job is not None
        assert job.state is PurchaseState.AWAITING_CONFIRMATION

    def test_the_review_carries_the_real_total(self, harness, rules) -> None:
        self._prepare(harness, rules)
        review = harness.events["ready"][0]
        assert review.order_total == usd("117.94")
        assert review.can_confirm
        assert review.report.passed

    def test_confirming_places_the_order(self, harness, rules) -> None:
        job_id = self._prepare(harness, rules)
        harness.service.confirm(job_id)

        assert len(harness.adapter.submitted) == 1
        authorisation = harness.adapter.submitted[0]
        assert authorisation.test_mode is False
        assert authorisation.guard_passed is True
        assert authorisation.approved_total == usd("117.94")

        job = harness.purchases.get(job_id)
        assert job is not None and job.state is PurchaseState.CONFIRMED
        assert harness.events["completed"]
        order = harness.events["completed"][0]
        assert order.amazon_order_number == "112-1234567-7654321"
        assert order.total == usd("117.94")

    def test_confirming_twice_is_refused(self, harness, rules) -> None:
        job_id = self._prepare(harness, rules)
        harness.service.confirm(job_id)
        with pytest.raises(AppError):
            harness.service.confirm(job_id)
        assert len(harness.adapter.submitted) == 1

    def test_confirming_an_unknown_job_is_refused(self, harness, rules) -> None:
        self._prepare(harness, rules)
        with pytest.raises(AppError):
            harness.service.confirm(9999)

    def test_declining_leaves_nothing_ordered(self, harness, rules) -> None:
        job_id = self._prepare(harness, rules)
        harness.service.cancel(job_id)
        assert harness.adapter.submitted == []
        assert harness.service.pending_review(job_id) is None

    def test_switching_test_mode_on_before_confirming_stops_the_order(
        self, harness, rules
    ) -> None:
        """Test Mode is read live, at the moment of submission.

        A user who switches it on while the confirmation is on screen has
        said "do not order". Taking the value frozen when the purchase
        started would order anyway.
        """
        job_id = self._prepare(harness, rules)
        harness.settings.update(test_mode=True)

        harness.service.confirm(job_id)

        assert harness.adapter.submitted == [], "nothing may be submitted"
        assert not harness.purchases.has_submitted(job_id)
        job = harness.purchases.get(job_id)
        assert job is not None and job.state is PurchaseState.CANCELLED
        assert job.outcome_code == "test_mode"
        assert harness.events["cancelled"] == [job_id]
        assert harness.events["failed"] == [], "this is not an error"
        assert harness.adapter.restored >= 1, "the cart must be put back"

    def test_the_progress_messages_are_human(self, harness, rules) -> None:
        """The step text a user watches must read as English."""
        self._prepare(harness, rules)
        messages = harness.worker.session.steps
        assert messages, "the run should have reported its steps"
        for message in messages:
            assert "_" not in message, message
            assert message[0].isupper(), message
            # No state names or error codes leaking into the UI.
            assert "product_check" not in message
            assert "rule_validation" not in message


# ---------------------------------------------------------------------------
# Automatic purchases
# ---------------------------------------------------------------------------


class TestAutomaticPurchase:
    def test_requires_consent_first(self, harness, rules) -> None:
        harness.settings.update(test_mode=False, auto_buy_acknowledged=False)
        with pytest.raises(AppError) as excinfo:
            harness.service.start(
                product_id=harness.product_id,
                rules=rules,
                mode=PurchaseMode.AUTOMATIC,
            )
        assert "not been switched on" in excinfo.value.detail
        assert harness.adapter.submitted == []

    def test_buys_without_asking_once_allowed(self, harness, rules) -> None:
        harness.settings.update(test_mode=False, auto_buy_acknowledged=True)
        job_id = harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.AUTOMATIC
        )
        assert harness.events["ready"] == [], "automatic mode must not ask"
        assert len(harness.adapter.submitted) == 1
        job = harness.purchases.get(job_id)
        assert job is not None and job.state is PurchaseState.CONFIRMED

    def test_uses_the_same_authorisation_as_assisted(self, harness, rules) -> None:
        """The whole point: one code path, one guard."""
        harness.settings.update(test_mode=False, auto_buy_acknowledged=True)
        harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.AUTOMATIC
        )
        authorisation = harness.adapter.submitted[0]
        assert isinstance(authorisation, SubmitAuthorization)
        assert authorisation.guard_passed is True
        assert authorisation.test_mode is False
        assert authorisation.approved_total == usd("117.94")

    def test_test_mode_still_wins_over_automatic(self, harness, rules) -> None:
        harness.settings.update(test_mode=True, auto_buy_acknowledged=True)
        harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.AUTOMATIC
        )
        assert harness.adapter.submitted == []
        assert harness.events["test"]

    def test_a_failing_guard_blocks_automatic_too(self, harness, rules) -> None:
        harness.settings.update(test_mode=False, auto_buy_acknowledged=True)
        harness.adapter.snapshot = replace(
            harness.adapter.snapshot, price=usd("999.00")
        )
        harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.AUTOMATIC
        )
        assert harness.adapter.submitted == []
        assert harness.events["blocked"]
        assert harness.events["blocked"][0].error.code is ErrorCode.PRICE_ABOVE_LIMIT


# ---------------------------------------------------------------------------
# Blocking
# ---------------------------------------------------------------------------


class TestBlocking:
    @pytest.mark.parametrize(
        ("field", "value", "expected_code"),
        [
            ("price", None, ErrorCode.PRICE_UNAVAILABLE),
            ("seller", "XYZ Marketplace LLC", ErrorCode.SELLER_NOT_ALLOWED),
            ("condition", ItemCondition.USED, ErrorCode.CONDITION_NOT_ALLOWED),
            ("availability", Availability.OUT_OF_STOCK, ErrorCode.PRODUCT_UNAVAILABLE),
            ("subscription_preselected", True, ErrorCode.SUBSCRIPTION_DETECTED),
        ],
    )
    def test_a_bad_product_blocks_before_any_cart_change(
        self, harness, rules, field, value, expected_code
    ) -> None:
        harness.settings.update(test_mode=False)
        harness.adapter.snapshot = replace(
            harness.adapter.snapshot, **{field: value}
        )
        job_id = harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        assert harness.adapter.submitted == []
        assert harness.events["blocked"]
        job = harness.purchases.get(job_id)
        assert job is not None and job.state is PurchaseState.BLOCKED
        assert job.outcome_code is not None

    def test_a_polluted_cart_blocks_with_the_item_names(self, harness, rules) -> None:
        harness.settings.update(test_mode=False)
        harness.adapter.snapshot = replace(
            harness.adapter.snapshot, buy_now_available=False
        )
        harness.adapter.cart = CartState(
            lines=(
                CartLine(asin="B0DOGFOOD01", title="Large bag of dog food", quantity=2),
            )
        )
        harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        outcome = harness.events["blocked"][0]
        assert outcome.error.code is ErrorCode.CART_CONFLICT
        # The UI needs the names to offer to set them aside.
        assert "Large bag of dog food" in outcome.error.context["unexpected"]
        assert harness.adapter.submitted == []

    def test_an_over_limit_total_blocks_at_the_last_gate(
        self, harness, rules, checkout
    ) -> None:
        harness.settings.update(test_mode=False)
        harness.adapter.checkout = replace(checkout, order_total=usd("200.00"))
        harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        outcome = harness.events["blocked"][0]
        assert outcome.error.code is ErrorCode.TOTAL_ABOVE_LIMIT
        assert harness.adapter.submitted == []

    def test_a_blocked_purchase_is_written_to_the_activity_feed(
        self, harness, rules
    ) -> None:
        harness.settings.update(test_mode=False)
        harness.adapter.snapshot = replace(harness.adapter.snapshot, price=usd("999.00"))
        harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        events = harness.activity.list_events()
        assert any("blocked" in event.title.lower() for event in events)


# ---------------------------------------------------------------------------
# Uncertain outcomes
# ---------------------------------------------------------------------------


class TestUncertainOutcome:
    def _submit_with_unreadable_confirmation(self, harness, rules) -> int:
        harness.settings.update(test_mode=False, auto_buy_acknowledged=True)
        harness.adapter.confirmation = OrderConfirmation(verified=False)
        return harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.AUTOMATIC
        )

    def test_an_unreadable_confirmation_becomes_uncertain(
        self, harness, rules
    ) -> None:
        job_id = self._submit_with_unreadable_confirmation(harness, rules)
        job = harness.purchases.get(job_id)
        assert job is not None and job.state is PurchaseState.UNKNOWN
        assert job.outcome_code == ErrorCode.ORDER_RESULT_UNCERTAIN.value
        assert harness.events["uncertain"] == [job_id]
        assert harness.events["completed"] == []

    def test_no_order_is_recorded_for_an_uncertain_outcome(
        self, harness, rules
    ) -> None:
        self._submit_with_unreadable_confirmation(harness, rules)
        assert harness.orders.list_recent() == []

    def test_it_is_never_retried_automatically(self, harness, rules) -> None:
        self._submit_with_unreadable_confirmation(harness, rules)
        submissions = len(harness.adapter.submitted)
        # Any further attempt on the same product must be refused outright.
        with pytest.raises(AppError) as excinfo:
            harness.service.start(
                product_id=harness.product_id,
                rules=rules,
                mode=PurchaseMode.AUTOMATIC,
            )
        assert excinfo.value.code is ErrorCode.DUPLICATE_BLOCKED
        assert len(harness.adapter.submitted) == submissions

    def test_the_user_can_resolve_it_as_placed(self, harness, rules) -> None:
        job_id = self._submit_with_unreadable_confirmation(harness, rules)
        order = harness.service.resolve_uncertain(
            job_id, order_was_placed=True, note="Found it in my orders"
        )
        assert order is not None
        job = harness.purchases.get(job_id)
        assert job is not None and job.state is PurchaseState.CONFIRMED

    def test_the_user_can_resolve_it_as_not_placed(self, harness, rules) -> None:
        job_id = self._submit_with_unreadable_confirmation(harness, rules)
        harness.service.resolve_uncertain(
            job_id, order_was_placed=False, note="No order in my history"
        )
        job = harness.purchases.get(job_id)
        assert job is not None and job.state is PurchaseState.FAILED
        assert harness.orders.list_recent() == []

    def test_resolving_frees_the_product(self, harness, rules) -> None:
        job_id = self._submit_with_unreadable_confirmation(harness, rules)
        harness.service.resolve_uncertain(
            job_id, order_was_placed=False, note="checked"
        )
        harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.AUTOMATIC
        )

    def test_a_submit_failure_after_recording_is_uncertain_not_failed(
        self, harness, rules
    ) -> None:
        """A crash mid-click may still have produced an order."""
        harness.settings.update(test_mode=False, auto_buy_acknowledged=True)
        harness.adapter.submit_error = AppError(ErrorCode.TIMEOUT)
        job_id = harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.AUTOMATIC
        )
        job = harness.purchases.get(job_id)
        assert job is not None
        assert job.state is PurchaseState.UNKNOWN
        assert harness.events["uncertain"] == [job_id]

    def test_an_uncertain_job_cannot_be_cancelled_away(self, harness, rules) -> None:
        """Cancelling must not hide the fact that an order may exist."""
        job_id = self._submit_with_unreadable_confirmation(harness, rules)
        harness.service.cancel(job_id)
        job = harness.purchases.get(job_id)
        assert job is not None and job.state is PurchaseState.UNKNOWN


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


class TestRecovery:
    def test_recovery_reports_uncertain_jobs(self, harness, rules) -> None:
        harness.settings.update(test_mode=False, auto_buy_acknowledged=True)
        harness.worker.defer = True
        job_id = harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.AUTOMATIC
        )
        # Simulate dying mid-submission.
        for state in (
            PurchaseState.PRODUCT_CHECK,
            PurchaseState.RULE_VALIDATION,
            PurchaseState.CART_PREPARATION,
            PurchaseState.CHECKOUT,
            PurchaseState.FINAL_VALIDATION,
            PurchaseState.SUBMITTING,
        ):
            harness.purchases.transition(job_id, state)

        uncertain = harness.service.recover_after_restart()
        assert uncertain == [job_id]
        job = harness.purchases.get(job_id)
        assert job is not None and job.state is PurchaseState.UNKNOWN
        assert any(
            "interrupted" in event.title.lower()
            for event in harness.activity.list_events()
        )

    def test_recovery_never_says_nothing_was_ordered_after_a_submission(
        self, harness, rules
    ) -> None:
        """A recorded submission means UNKNOWN, whatever state the row is in.

        ``FAILED`` would claim nothing was ordered, free the product's
        single-in-flight slot and never prompt the user, which is exactly how
        a crash could become a second order.
        """
        harness.settings.update(test_mode=False)
        harness.worker.defer = True
        job_id = harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        for state in (
            PurchaseState.PRODUCT_CHECK,
            PurchaseState.RULE_VALIDATION,
            PurchaseState.CART_PREPARATION,
            PurchaseState.CHECKOUT,
            PurchaseState.FINAL_VALIDATION,
            PurchaseState.AWAITING_CONFIRMATION,
        ):
            harness.purchases.transition(job_id, state)
        attempt = harness.purchases.begin_attempt(job_id)
        harness.purchases.mark_submitted(attempt)

        assert harness.service.recover_after_restart() == [job_id]
        job = harness.purchases.get(job_id)
        assert job is not None and job.state is PurchaseState.UNKNOWN
        assert "Nothing was ordered" not in (job.outcome_detail or "")
        # The product is still held, so nothing can start a second order.
        with pytest.raises(AppError):
            harness.service.start(
                product_id=harness.product_id, rules=rules, mode=PurchaseMode.ASSISTED
            )

    def test_recovery_fails_jobs_that_never_submitted(self, harness, rules) -> None:
        harness.settings.update(test_mode=False)
        harness.worker.defer = True
        job_id = harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        harness.purchases.transition(job_id, PurchaseState.PRODUCT_CHECK)

        assert harness.service.recover_after_restart() == []
        job = harness.purchases.get(job_id)
        assert job is not None and job.state is PurchaseState.FAILED
        assert "Nothing was ordered" in (job.outcome_detail or "")


# ---------------------------------------------------------------------------
# The pre-checkout guard phase
# ---------------------------------------------------------------------------


class TestPreCheckoutGuard:
    """The cart route must be validated before the checkout is entered.

    This is the last point at which stopping costs nothing: the item is in a
    cart, no checkout has been opened, and the cart can be put back exactly
    as it was found.
    """

    @pytest.fixture
    def cart_route(self, harness, rules):
        """Force the add-to-cart route by removing Buy Now."""
        harness.settings.update(test_mode=False)
        harness.adapter.snapshot = replace(
            harness.adapter.snapshot,
            buy_now_available=False,
            add_to_cart_available=True,
        )
        return harness

    def test_the_phase_runs_and_is_recorded(self, cart_route, rules) -> None:
        harness = cart_route
        job_id = harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        assert harness.adapter.cart_guard_calls == 1
        report = harness.purchases.latest_guard_report(job_id, GuardPhase.PRE_CHECKOUT)
        assert report is not None, "the pre-checkout report must be persisted"
        assert report["passed"] is True
        assert report["checks"], "the report must list the checks it ran"
        assert harness.events["ready"], "a passing cart proceeds as normal"

    def test_an_unrelated_line_blocks_before_the_checkout(
        self, cart_route, rules
    ) -> None:
        harness = cart_route
        harness.adapter.prepared_cart = CartState(
            lines=(
                CartLine(
                    asin=ASIN,
                    title="Clamp meter",
                    quantity=1,
                    unit_price=usd("109.97"),
                ),
                CartLine(
                    asin="B00OTHER11",
                    title="Something else entirely",
                    quantity=1,
                    unit_price=usd("42.00"),
                ),
            ),
            subtotal=usd("151.97"),
        )
        job_id = harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.ASSISTED
        )

        assert harness.adapter.submitted == []
        assert harness.events["ready"] == [], "a blocked cart never asks to buy"
        job = harness.purchases.get(job_id)
        assert job is not None and job.state is PurchaseState.BLOCKED
        assert job.outcome_code == ErrorCode.UNEXPECTED_CART_ITEMS.value
        assert harness.adapter.restored >= 1, "the cart must be put back"
        outcome = harness.events["blocked"][0]
        assert outcome.report is not None
        assert outcome.report.phase is GuardPhase.PRE_CHECKOUT

    def test_a_quantity_the_page_changed_blocks(self, cart_route, rules) -> None:
        harness = cart_route
        harness.adapter.prepared_cart = CartState(
            lines=(
                CartLine(
                    asin=ASIN,
                    title="Clamp meter",
                    quantity=3,
                    unit_price=usd("109.97"),
                ),
            ),
            subtotal=usd("329.91"),
        )
        job_id = harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        assert harness.adapter.submitted == []
        job = harness.purchases.get(job_id)
        assert job is not None and job.state is PurchaseState.BLOCKED

    def test_an_unreadable_quantity_blocks(self, cart_route, rules) -> None:
        """``None`` means "not read", which may never be treated as correct."""
        harness = cart_route
        harness.adapter.prepared_cart = CartState(
            lines=(
                CartLine(
                    asin=ASIN,
                    title="Clamp meter",
                    quantity=None,
                    unit_price=usd("109.97"),
                ),
            ),
            subtotal=usd("109.97"),
        )
        job_id = harness.service.start(
            product_id=harness.product_id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        assert harness.adapter.submitted == []
        job = harness.purchases.get(job_id)
        assert job is not None and job.state is PurchaseState.BLOCKED


# ---------------------------------------------------------------------------
# The monitor service
# ---------------------------------------------------------------------------


class FakeScheduler(QObject):
    tick = Signal()
    resumed = Signal(float)

    def __init__(self) -> None:
        super().__init__()
        self.running = False

    @property
    def is_running(self) -> bool:
        return self.running

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False


@pytest.fixture
def monitor(harness, rules, monkeypatch):
    from app.monitoring.monitor_service import MonitorService

    monkeypatch.setattr("app.monitoring.monitor_service.ADAPTER", harness.adapter)
    scheduler = FakeScheduler()
    stored = harness.rules_repo.create(rules)
    assert stored.rules_id is not None
    service = MonitorService(
        worker=harness.worker,
        scheduler=scheduler,
        watches=harness.watches,
        products=harness.products,
        rules_repo=harness.rules_repo,
        purchases=harness.purchases,
        activity=harness.activity,
        settings=harness.settings,
        purchase_service=harness.service,
        notifier=None,
    )
    return service, scheduler, stored.rules_id


class TestMonitorService:
    def test_a_check_records_an_observation_and_reschedules(
        self, harness, monitor
    ) -> None:
        service, scheduler, rules_id = monitor
        job = harness.watches.create(
            product_id=harness.product_id,
            rules_id=rules_id,
            action=WatchAction.NOTIFY,
            interval_seconds=300,
        )
        service.check_now(job.id)

        updated = harness.watches.get(job.id)
        assert updated is not None
        assert updated.checks_performed == 1
        # $109.97 is at or below the fixture's $120 limit, so the target is met.
        assert updated.status is WatchStatus.TARGET_REACHED
        assert updated.last_check_summary
        assert harness.products.latest_observation(harness.product_id) is not None
        assert harness.watches.due_jobs() == []

    def test_a_price_above_the_target_reports_waiting(
        self, harness, monitor, rules
    ) -> None:
        service, _scheduler, _rules_id = monitor
        strict = harness.rules_repo.create(replace(rules, max_item_price=usd("50.00")))
        assert strict.rules_id is not None
        job = harness.watches.create(
            product_id=harness.product_id,
            rules_id=strict.rules_id,
            action=WatchAction.NOTIFY,
            interval_seconds=300,
        )
        service.check_now(job.id)
        updated = harness.watches.get(job.id)
        assert updated is not None
        assert updated.status is WatchStatus.WAITING_FOR_PRICE
        assert "waiting for $50.00" in (updated.last_check_summary or "")

    def test_a_notify_watch_never_buys(self, harness, monitor, rules) -> None:
        service, scheduler, _rules_id = monitor
        harness.settings.update(test_mode=False, auto_buy_acknowledged=True)
        cheap = harness.rules_repo.create(replace(rules, max_item_price=usd("500.00")))
        assert cheap.rules_id is not None
        job = harness.watches.create(
            product_id=harness.product_id,
            rules_id=cheap.rules_id,
            action=WatchAction.NOTIFY,
            interval_seconds=300,
        )
        service.check_now(job.id)
        assert harness.adapter.submitted == []

    def test_a_buy_watch_hands_over_to_the_purchase_service(
        self, harness, monitor, rules
    ) -> None:
        service, scheduler, _rules_id = monitor
        harness.settings.update(test_mode=False, auto_buy_acknowledged=True)
        buyable = harness.rules_repo.create(
            replace(rules, max_item_price=usd("500.00"), max_order_total=usd("500.00"))
        )
        assert buyable.rules_id is not None
        job = harness.watches.create(
            product_id=harness.product_id,
            rules_id=buyable.rules_id,
            action=WatchAction.BUY,
            interval_seconds=300,
        )
        service.check_now(job.id)

        assert len(harness.adapter.submitted) == 1
        finished = harness.watches.get(job.id)
        assert finished is not None
        assert finished.status is WatchStatus.PURCHASE_COMPLETED
        assert not finished.status.is_active

    def test_a_completed_watch_is_never_checked_again(
        self, harness, monitor, rules
    ) -> None:
        """This is what stops an auto-buy watch ordering twice."""
        service, scheduler, _rules_id = monitor
        harness.settings.update(test_mode=False, auto_buy_acknowledged=True)
        buyable = harness.rules_repo.create(
            replace(rules, max_item_price=usd("500.00"), max_order_total=usd("500.00"))
        )
        assert buyable.rules_id is not None
        job = harness.watches.create(
            product_id=harness.product_id,
            rules_id=buyable.rules_id,
            action=WatchAction.BUY,
            interval_seconds=300,
        )
        service.check_now(job.id)
        submissions = len(harness.adapter.submitted)

        assert harness.watches.due_jobs() == []
        scheduler.tick.emit()
        assert len(harness.adapter.submitted) == submissions

    def test_a_buy_watch_respects_test_mode(self, harness, monitor, rules) -> None:
        service, scheduler, _rules_id = monitor
        harness.settings.update(test_mode=True, auto_buy_acknowledged=True)
        buyable = harness.rules_repo.create(
            replace(rules, max_item_price=usd("500.00"), max_order_total=usd("500.00"))
        )
        assert buyable.rules_id is not None
        job = harness.watches.create(
            product_id=harness.product_id,
            rules_id=buyable.rules_id,
            action=WatchAction.BUY,
            interval_seconds=300,
        )
        service.check_now(job.id)
        assert harness.adapter.submitted == []

    def test_throttling_backs_every_watch_off(self, harness, monitor) -> None:
        service, scheduler, rules_id = monitor
        job = harness.watches.create(
            product_id=harness.product_id,
            rules_id=rules_id,
            action=WatchAction.NOTIFY,
            interval_seconds=300,
        )
        harness.adapter.inspect_error = AppError(ErrorCode.RATE_LIMITED)
        service.check_now(job.id)

        updated = harness.watches.get(job.id)
        assert updated is not None
        assert updated.consecutive_failures == 1
        assert harness.watches.due_jobs() == []

    def test_a_verification_challenge_pauses_monitoring(
        self, harness, monitor
    ) -> None:
        """Never poll into a challenge."""
        service, scheduler, rules_id = monitor
        service.start()
        assert service.is_monitoring
        job = harness.watches.create(
            product_id=harness.product_id,
            rules_id=rules_id,
            action=WatchAction.NOTIFY,
            interval_seconds=300,
        )
        harness.adapter.inspect_error = AppError(ErrorCode.VERIFICATION_REQUIRED)
        service.check_now(job.id)

        assert not service.is_monitoring
        updated = harness.watches.get(job.id)
        assert updated is not None
        assert updated.status is WatchStatus.NEEDS_VERIFICATION

    def test_an_expired_login_marks_the_watch(self, harness, monitor) -> None:
        service, scheduler, rules_id = monitor
        job = harness.watches.create(
            product_id=harness.product_id,
            rules_id=rules_id,
            action=WatchAction.NOTIFY,
            interval_seconds=300,
        )
        harness.adapter.inspect_error = AppError(ErrorCode.LOGIN_EXPIRED)
        service.check_now(job.id)
        updated = harness.watches.get(job.id)
        assert updated is not None
        assert updated.status is WatchStatus.NEEDS_LOGIN

    def test_pausing_stops_it_being_due(self, harness, monitor) -> None:
        service, scheduler, rules_id = monitor
        job = harness.watches.create(
            product_id=harness.product_id,
            rules_id=rules_id,
            action=WatchAction.NOTIFY,
            interval_seconds=300,
        )
        service.pause_watch(job.id)
        assert harness.watches.due_jobs() == []
        service.resume_watch(job.id)
        updated = harness.watches.get(job.id)
        assert updated is not None and updated.status is WatchStatus.WATCHING

    def test_resuming_from_sleep_reschedules_rather_than_bursting(
        self, harness, monitor, rules
    ) -> None:
        service, scheduler, rules_id = monitor
        for index in range(3):
            snapshot = replace(
                harness.adapter.snapshot, asin=f"B0SLEEP{index:04d}"
            )
            record = harness.products.upsert_from_snapshot(snapshot)
            stored = harness.rules_repo.create(
                replace(rules, expected_asin=snapshot.asin)
            )
            assert stored.rules_id is not None
            harness.watches.create(
                product_id=record.id,
                rules_id=stored.rules_id,
                action=WatchAction.NOTIFY,
                interval_seconds=300,
            )
        assert len(harness.watches.due_jobs(limit=10)) == 3

        scheduler.resumed.emit(3600.0)
        # Everything is pushed into the near future, staggered, rather than
        # all firing at once.
        assert harness.watches.due_jobs(limit=10) == []

    def test_monitoring_can_be_switched_off_and_on(self, harness, monitor) -> None:
        service, scheduler, _rules_id = monitor
        service.set_enabled(False)
        assert not service.is_monitoring
        assert harness.settings.current.monitoring_enabled is False
        service.set_enabled(True)
        assert service.is_monitoring
        assert harness.settings.current.monitoring_enabled is True
