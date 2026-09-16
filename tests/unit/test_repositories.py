"""Tests for the repository layer.

``TestDuplicateProtection`` and ``TestCrashRecovery`` are the purchase-safety
tests at the persistence level: they try, through the repository API, to get
two orders out of one intent.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.core.errors import AppError, ErrorCode
from app.core.money import Money
from app.database.database import Database
from app.database.records import (
    ActivityCategory,
    ActivitySeverity,
    ConfirmationStatus,
    WatchAction,
    WatchStatus,
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
    Availability,
    CartLine,
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
from app.purchasing.states import IllegalTransition, PurchaseState
from app.purchasing.validation import GuardPhase

ASIN = "B07XYZ1234"


def usd(amount: str) -> Money:
    return Money.from_decimal(amount, "USD")


@pytest.fixture
def products(database: Database) -> ProductRepository:
    return ProductRepository(database)


@pytest.fixture
def rules_repo(database: Database) -> RulesRepository:
    return RulesRepository(database)


@pytest.fixture
def watches(database: Database) -> WatchRepository:
    return WatchRepository(database)


@pytest.fixture
def purchases(database: Database) -> PurchaseRepository:
    return PurchaseRepository(database)


@pytest.fixture
def orders(database: Database) -> OrderRepository:
    return OrderRepository(database)


@pytest.fixture
def activity(database: Database) -> ActivityRepository:
    return ActivityRepository(database)


@pytest.fixture
def snapshot() -> ProductSnapshot:
    return ProductSnapshot(
        asin=ASIN,
        title="Klein Tools CL800 Clamp Meter",
        brand="Klein Tools",
        image_url="https://example.invalid/i.jpg",
        url=f"https://www.amazon.com/dp/{ASIN}",
        price=usd("109.97"),
        availability=Availability.IN_STOCK,
        availability_text="In Stock",
        seller="Amazon.com",
        ships_from="Amazon.com",
        condition=ItemCondition.NEW,
        variation=VariationSnapshot({"Color": "Black"}),
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
        approved_sellers=("Tool Depot",),
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
        payment_label="Visa ending 1234",
        place_order_control_found=True,
    )


class TestProducts:
    def test_upsert_creates_then_updates(
        self, products: ProductRepository, snapshot: ProductSnapshot
    ) -> None:
        first = products.upsert_from_snapshot(snapshot)
        assert first.asin == ASIN
        assert first.title == snapshot.title

        from dataclasses import replace

        second = products.upsert_from_snapshot(
            replace(snapshot, title="Renamed product")
        )
        assert second.id == first.id
        assert second.title == "Renamed product"

    def test_upsert_does_not_erase_known_fields(
        self, products: ProductRepository, snapshot: ProductSnapshot
    ) -> None:
        """A partial parse must not wipe a good title captured earlier."""
        from dataclasses import replace

        products.upsert_from_snapshot(snapshot)
        products.upsert_from_snapshot(
            replace(snapshot, title=None, image_url=None, brand=None)
        )
        stored = products.find_by_asin(ASIN)
        assert stored is not None
        assert stored.title == snapshot.title
        assert stored.image_url == snapshot.image_url

    def test_asin_lookup_is_case_insensitive(
        self, products: ProductRepository, snapshot: ProductSnapshot
    ) -> None:
        products.upsert_from_snapshot(snapshot)
        assert products.find_by_asin(ASIN.lower()) is not None

    def test_observation_and_latest(
        self, products: ProductRepository, snapshot: ProductSnapshot
    ) -> None:
        product = products.upsert_from_snapshot(snapshot)
        products.record_observation(product.id, snapshot)
        latest = products.latest_observation(product.id)
        assert latest is not None
        assert latest.price == usd("109.97")
        assert latest.seller == "Amazon.com"
        assert latest.condition is ItemCondition.NEW

    def test_unknown_availability_is_stored_as_null(
        self, products: ProductRepository, snapshot: ProductSnapshot
    ) -> None:
        from dataclasses import replace

        product = products.upsert_from_snapshot(snapshot)
        products.record_observation(
            product.id, replace(snapshot, availability=Availability.UNKNOWN)
        )
        latest = products.latest_observation(product.id)
        assert latest is not None
        assert latest.in_stock is None
        assert latest.availability is Availability.UNKNOWN

    def test_history_is_pruned(
        self, products: ProductRepository, snapshot: ProductSnapshot, monkeypatch
    ) -> None:
        import app.database.repositories.products as module

        monkeypatch.setattr(module, "PRICE_HISTORY_LIMIT", 5)
        product = products.upsert_from_snapshot(snapshot)
        for _ in range(12):
            products.record_observation(product.id, snapshot)
        assert len(products.history(product.id, limit=100)) == 5

    def test_price_series_is_oldest_first(
        self, products: ProductRepository, snapshot: ProductSnapshot
    ) -> None:
        from dataclasses import replace

        product = products.upsert_from_snapshot(snapshot)
        for amount in ("100.00", "90.00", "80.00"):
            products.record_observation(
                product.id, replace(snapshot, price=usd(amount))
            )
        series = products.price_series(product.id)
        assert [obs.price.cents for obs in series if obs.price] == [10000, 9000, 8000]

    def test_variation_recorded_once(
        self, products: ProductRepository, snapshot: ProductSnapshot, database: Database
    ) -> None:
        product = products.upsert_from_snapshot(snapshot)
        products.record_variation(product.id, snapshot.variation)
        products.record_variation(product.id, snapshot.variation)
        count = database.query_scalar(
            "SELECT COUNT(*) FROM product_variations WHERE product_id = ?",
            (product.id,),
        )
        assert count == 1


class TestRules:
    def test_round_trip(self, rules_repo: RulesRepository, rules: PurchaseRules) -> None:
        stored = rules_repo.create(rules)
        assert stored.rules_id is not None
        assert stored.max_item_price == usd("120.00")
        assert stored.max_order_total == usd("135.00")
        assert stored.seller_policy is SellerPolicy.AMAZON_ONLY
        assert stored.condition_policy is ConditionPolicy.NEW_ONLY
        assert stored.expected_variation.dimensions == {"Color": "Black"}
        assert stored.approved_sellers == ("Tool Depot",)
        assert stored.expected_asin == ASIN

    def test_optional_limits_may_be_absent(self, rules_repo: RulesRepository) -> None:
        stored = rules_repo.create(PurchaseRules(expected_asin=ASIN))
        assert stored.max_item_price is None
        assert stored.max_order_total is None

    def test_update_replaces_fields(
        self, rules_repo: RulesRepository, rules: PurchaseRules
    ) -> None:
        from dataclasses import replace

        stored = rules_repo.create(rules)
        assert stored.rules_id is not None
        updated = rules_repo.update(
            stored.rules_id, replace(rules, quantity=4, max_item_price=usd("99.00"))
        )
        assert updated.quantity == 4
        assert updated.max_item_price == usd("99.00")

    def test_duplicate_makes_an_independent_copy(
        self, rules_repo: RulesRepository, rules: PurchaseRules
    ) -> None:
        from dataclasses import replace

        original = rules_repo.create(rules)
        assert original.rules_id is not None
        copy = rules_repo.duplicate(original.rules_id)
        assert copy.rules_id != original.rules_id

        rules_repo.update(copy.rules_id, replace(rules, quantity=9))
        unchanged = rules_repo.get(original.rules_id)
        assert unchanged is not None
        assert unchanged.quantity == 1


class TestWatchJobs:
    def _setup(self, products, rules_repo, watches, snapshot, rules, **kwargs):
        product = products.upsert_from_snapshot(snapshot)
        stored_rules = rules_repo.create(rules)
        assert stored_rules.rules_id is not None
        job = watches.create(
            product_id=product.id,
            rules_id=stored_rules.rules_id,
            action=kwargs.pop("action", WatchAction.NOTIFY),
            interval_seconds=kwargs.pop("interval_seconds", 300),
            **kwargs,
        )
        return product, stored_rules, job

    def test_create_and_view(
        self, products, rules_repo, watches, snapshot, rules
    ) -> None:
        _, _, job = self._setup(products, rules_repo, watches, snapshot, rules)
        view = watches.view(job.id)
        assert view is not None
        assert view.product.asin == ASIN
        assert view.rules.max_item_price == usd("120.00")
        assert view.job.status is WatchStatus.WATCHING

    def test_brand_is_supplied_from_the_product(
        self, products, rules_repo, watches, snapshot, rules
    ) -> None:
        """The manufacturer seller policy needs the brand, stored on products."""
        _, _, job = self._setup(products, rules_repo, watches, snapshot, rules)
        view = watches.view(job.id)
        assert view is not None
        assert view.rules.brand == "Klein Tools"

    def test_interval_floor_is_enforced(
        self, products, rules_repo, watches, snapshot, rules
    ) -> None:
        from app.config import MIN_CHECK_INTERVAL_SECONDS

        _, _, job = self._setup(
            products, rules_repo, watches, snapshot, rules, interval_seconds=5
        )
        assert job.interval_seconds >= MIN_CHECK_INTERVAL_SECONDS

    def test_due_jobs_returns_new_job_immediately(
        self, products, rules_repo, watches, snapshot, rules
    ) -> None:
        _, _, job = self._setup(products, rules_repo, watches, snapshot, rules)
        assert [item.id for item in watches.due_jobs()] == [job.id]

    def test_successful_check_reschedules(
        self, products, rules_repo, watches, snapshot, rules
    ) -> None:
        _, _, job = self._setup(products, rules_repo, watches, snapshot, rules)
        watches.record_check_success(
            job.id, summary="In stock at $109.97", status=WatchStatus.WAITING_FOR_PRICE
        )
        assert watches.due_jobs() == []
        updated = watches.get(job.id)
        assert updated is not None
        assert updated.checks_performed == 1
        assert updated.consecutive_failures == 0
        assert updated.status is WatchStatus.WAITING_FOR_PRICE

    def test_failure_applies_backoff(
        self, products, rules_repo, watches, snapshot, rules
    ) -> None:
        _, _, job = self._setup(products, rules_repo, watches, snapshot, rules)
        first = watches.record_check_failure(
            job.id, summary="Timed out", error_code="timeout"
        )
        updated = watches.get(job.id)
        assert updated is not None
        assert updated.consecutive_failures == 1
        assert first >= 300

        watches.record_check_failure(job.id, summary="Timed out", error_code="timeout")
        again = watches.get(job.id)
        assert again is not None
        assert again.consecutive_failures == 2

    def test_backoff_ladder_escalates_then_caps(self) -> None:
        from app.database.repositories.watch import BACKOFF_LADDER, backoff_delay

        assert backoff_delay(0) == 0
        values = [backoff_delay(n) for n in range(1, 9)]
        assert values[: len(BACKOFF_LADDER)] == list(BACKOFF_LADDER)
        assert all(value == BACKOFF_LADDER[-1] for value in values[len(BACKOFF_LADDER):])

    def test_jitter_stays_within_bounds(self) -> None:
        from app.config import CHECK_INTERVAL_JITTER, MIN_CHECK_INTERVAL_SECONDS
        from app.database.repositories.watch import jittered_delay

        for _ in range(200):
            value = jittered_delay(600)
            assert MIN_CHECK_INTERVAL_SECONDS <= value
            assert 600 * (1 - CHECK_INTERVAL_JITTER) - 1 <= value
            assert value <= 600 * (1 + CHECK_INTERVAL_JITTER) + 1

    def test_pause_removes_from_due_and_resume_restores(
        self, products, rules_repo, watches, snapshot, rules
    ) -> None:
        _, _, job = self._setup(products, rules_repo, watches, snapshot, rules)
        watches.pause(job.id)
        assert watches.due_jobs() == []
        paused = watches.get(job.id)
        assert paused is not None and paused.status is WatchStatus.PAUSED

        watches.resume(job.id)
        resumed = watches.get(job.id)
        assert resumed is not None and resumed.status is WatchStatus.WATCHING

    def test_purchase_completed_stops_the_job_permanently(
        self, products, rules_repo, watches, snapshot, rules
    ) -> None:
        """This is what stops an auto-buy watch from ordering twice."""
        _, _, job = self._setup(
            products, rules_repo, watches, snapshot, rules, action=WatchAction.BUY
        )
        watches.mark_purchase_completed(job.id)
        assert watches.due_jobs() == []
        final = watches.get(job.id)
        assert final is not None
        assert final.status is WatchStatus.PURCHASE_COMPLETED
        assert not final.status.is_active

    def test_expiry(self, products, rules_repo, watches, snapshot, rules) -> None:
        _, _, job = self._setup(
            products,
            rules_repo,
            watches,
            snapshot,
            rules,
            expires_at="2020-01-01T00:00:00.000000Z",
        )
        assert watches.expire_due() == [job.id]
        expired = watches.get(job.id)
        assert expired is not None and expired.status is WatchStatus.EXPIRED
        assert watches.due_jobs() == []

    def test_defer_all_active_slows_everything(
        self, products, rules_repo, watches, snapshot, rules
    ) -> None:
        """A throttle signal is address-level, so every job slows down."""
        self._setup(products, rules_repo, watches, snapshot, rules)
        from dataclasses import replace

        second = products.upsert_from_snapshot(replace(snapshot, asin="B0SECOND001"))
        second_rules = rules_repo.create(replace(rules, expected_asin="B0SECOND001"))
        assert second_rules.rules_id is not None
        watches.create(
            product_id=second.id,
            rules_id=second_rules.rules_id,
            action=WatchAction.NOTIFY,
            interval_seconds=300,
        )
        assert len(watches.due_jobs()) == 2
        assert watches.defer_all_active(1800) == 2
        assert watches.due_jobs() == []

    def test_counts(self, products, rules_repo, watches, snapshot, rules) -> None:
        _, _, job = self._setup(products, rules_repo, watches, snapshot, rules)
        watches.set_status(job.id, WatchStatus.NEEDS_VERIFICATION)
        counts = watches.counts()
        assert counts["needs_attention"] == 1
        assert counts["total"] == 1

    def test_deleting_a_watch_leaves_no_rows(
        self, products, rules_repo, watches, snapshot, rules, database
    ) -> None:
        _, _, job = self._setup(products, rules_repo, watches, snapshot, rules)
        watches.delete(job.id)
        assert watches.get(job.id) is None
        assert database.query_scalar("SELECT COUNT(*) FROM watch_jobs") == 0


class TestPurchaseJobs:
    def _job(self, products, rules_repo, purchases, snapshot, rules, **kwargs):
        product = products.upsert_from_snapshot(snapshot)
        stored = rules_repo.create(rules)
        assert stored.rules_id is not None
        job = purchases.create(
            product_id=product.id,
            rules_id=stored.rules_id,
            mode=kwargs.pop("mode", PurchaseMode.ASSISTED),
            test_mode=kwargs.pop("test_mode", True),
            **kwargs,
        )
        return product, stored, job

    def test_create_starts_in_created(
        self, products, rules_repo, purchases, snapshot, rules
    ) -> None:
        _, _, job = self._job(products, rules_repo, purchases, snapshot, rules)
        assert job.state is PurchaseState.CREATED
        assert job.test_mode is True

    def test_legal_transition_sequence(
        self, products, rules_repo, purchases, snapshot, rules
    ) -> None:
        _, _, job = self._job(products, rules_repo, purchases, snapshot, rules)
        for state in (
            PurchaseState.PRODUCT_CHECK,
            PurchaseState.RULE_VALIDATION,
            PurchaseState.CART_PREPARATION,
            PurchaseState.CHECKOUT,
            PurchaseState.FINAL_VALIDATION,
            PurchaseState.AWAITING_CONFIRMATION,
        ):
            job = purchases.transition(job.id, state)
        assert job.state is PurchaseState.AWAITING_CONFIRMATION
        assert len(purchases.transitions(job.id)) == 7

    def test_illegal_transition_raises(
        self, products, rules_repo, purchases, snapshot, rules
    ) -> None:
        _, _, job = self._job(products, rules_repo, purchases, snapshot, rules)
        with pytest.raises(IllegalTransition):
            purchases.transition(job.id, PurchaseState.SUBMITTING)

    def test_same_state_transition_is_a_no_op(
        self, products, rules_repo, purchases, snapshot, rules
    ) -> None:
        _, _, job = self._job(products, rules_repo, purchases, snapshot, rules)
        before = len(purchases.transitions(job.id))
        purchases.transition(job.id, PurchaseState.CREATED)
        assert len(purchases.transitions(job.id)) == before

    def test_terminal_timestamp_is_set(
        self, products, rules_repo, purchases, snapshot, rules
    ) -> None:
        _, _, job = self._job(products, rules_repo, purchases, snapshot, rules)
        blocked = purchases.transition(
            job.id, PurchaseState.BLOCKED, reason="price above limit"
        )
        assert blocked.terminal_at is not None

    def test_guard_report_round_trip(
        self, products, rules_repo, purchases, snapshot, rules, checkout
    ) -> None:
        from app.purchasing.purchase_guard import GUARD

        _, _, job = self._job(products, rules_repo, purchases, snapshot, rules)
        report = GUARD.check_final(rules, snapshot, checkout)
        purchases.save_guard_report(job.id, report)

        stored = purchases.latest_guard_report(job.id, GuardPhase.PRE_SUBMIT)
        assert stored is not None
        assert stored["passed"] is True
        assert len(stored["checks"]) == len(report.checks)

    def test_failed_guard_report_records_the_code(
        self, products, rules_repo, purchases, snapshot, rules, checkout
    ) -> None:
        from dataclasses import replace

        from app.purchasing.purchase_guard import GUARD

        _, _, job = self._job(products, rules_repo, purchases, snapshot, rules)
        report = GUARD.check_final(
            rules, snapshot, replace(checkout, order_total=usd("999.00"))
        )
        purchases.save_guard_report(job.id, report)
        stored = purchases.latest_guard_report(job.id)
        assert stored is not None
        assert stored["passed"] is False
        assert stored["blocked_code"] == ErrorCode.TOTAL_ABOVE_LIMIT.value


class TestDuplicateProtection:
    def _advance_to_submit(self, purchases, job):
        for state in (
            PurchaseState.PRODUCT_CHECK,
            PurchaseState.RULE_VALIDATION,
            PurchaseState.CART_PREPARATION,
            PurchaseState.CHECKOUT,
            PurchaseState.FINAL_VALIDATION,
        ):
            job = purchases.transition(job.id, state)
        return job

    def test_second_live_job_for_a_product_is_refused(
        self, products, rules_repo, purchases, snapshot, rules
    ) -> None:
        product = products.upsert_from_snapshot(snapshot)
        stored = rules_repo.create(rules)
        assert stored.rules_id is not None
        purchases.create(
            product_id=product.id,
            rules_id=stored.rules_id,
            mode=PurchaseMode.ASSISTED,
            test_mode=False,
        )
        with pytest.raises(AppError) as excinfo:
            purchases.create(
                product_id=product.id,
                rules_id=stored.rules_id,
                mode=PurchaseMode.ASSISTED,
                test_mode=False,
            )
        assert excinfo.value.code is ErrorCode.DUPLICATE_BLOCKED

    def test_unresolved_uncertain_job_blocks_a_new_one(
        self, products, rules_repo, purchases, snapshot, rules
    ) -> None:
        product = products.upsert_from_snapshot(snapshot)
        stored = rules_repo.create(rules)
        assert stored.rules_id is not None
        job = purchases.create(
            product_id=product.id,
            rules_id=stored.rules_id,
            mode=PurchaseMode.AUTOMATIC,
            test_mode=False,
        )
        job = self._advance_to_submit(purchases, job)
        purchases.transition(job.id, PurchaseState.SUBMITTING)
        purchases.transition(job.id, PurchaseState.UNKNOWN)

        with pytest.raises(AppError) as excinfo:
            purchases.create(
                product_id=product.id,
                rules_id=stored.rules_id,
                mode=PurchaseMode.AUTOMATIC,
                test_mode=False,
            )
        assert excinfo.value.code is ErrorCode.DUPLICATE_BLOCKED
        assert "unconfirmed" in excinfo.value.detail.lower()

    def test_one_submission_per_job(
        self, products, rules_repo, purchases, snapshot, rules
    ) -> None:
        product = products.upsert_from_snapshot(snapshot)
        stored = rules_repo.create(rules)
        assert stored.rules_id is not None
        job = purchases.create(
            product_id=product.id,
            rules_id=stored.rules_id,
            mode=PurchaseMode.AUTOMATIC,
            test_mode=False,
        )
        job = self._advance_to_submit(purchases, job)

        first = purchases.begin_attempt(job.id)
        purchases.mark_submitted(first)
        assert purchases.has_submitted(job.id)

        second = purchases.begin_attempt(job.id)
        with pytest.raises(AppError) as excinfo:
            purchases.mark_submitted(second)
        assert excinfo.value.code is ErrorCode.DUPLICATE_BLOCKED

    def test_marking_the_same_attempt_twice_is_refused(
        self, products, rules_repo, purchases, snapshot, rules
    ) -> None:
        """A double-click must not slip through as a silent no-op."""
        product = products.upsert_from_snapshot(snapshot)
        stored = rules_repo.create(rules)
        assert stored.rules_id is not None
        job = purchases.create(
            product_id=product.id,
            rules_id=stored.rules_id,
            mode=PurchaseMode.AUTOMATIC,
            test_mode=False,
        )
        job = self._advance_to_submit(purchases, job)
        attempt = purchases.begin_attempt(job.id)
        purchases.mark_submitted(attempt)
        with pytest.raises(AppError) as excinfo:
            purchases.mark_submitted(attempt)
        assert excinfo.value.code is ErrorCode.DUPLICATE_BLOCKED

    def test_submission_from_a_wrong_state_is_refused(
        self, products, rules_repo, purchases, snapshot, rules
    ) -> None:
        product = products.upsert_from_snapshot(snapshot)
        stored = rules_repo.create(rules)
        assert stored.rules_id is not None
        job = purchases.create(
            product_id=product.id,
            rules_id=stored.rules_id,
            mode=PurchaseMode.AUTOMATIC,
            test_mode=False,
        )
        attempt = purchases.begin_attempt(job.id)
        with pytest.raises(AppError) as excinfo:
            purchases.mark_submitted(attempt)
        assert excinfo.value.code is ErrorCode.INTERNAL_ERROR

    def test_unsubmitted_attempts_are_unlimited(
        self, products, rules_repo, purchases, snapshot, rules
    ) -> None:
        product = products.upsert_from_snapshot(snapshot)
        stored = rules_repo.create(rules)
        assert stored.rules_id is not None
        job = purchases.create(
            product_id=product.id,
            rules_id=stored.rules_id,
            mode=PurchaseMode.ASSISTED,
            test_mode=True,
        )
        for _ in range(5):
            attempt = purchases.begin_attempt(job.id)
            purchases.finish_attempt(attempt, outcome="not_submitted")
        assert not purchases.has_submitted(job.id)

    def test_terminal_job_frees_the_product(
        self, products, rules_repo, purchases, snapshot, rules
    ) -> None:
        product = products.upsert_from_snapshot(snapshot)
        stored = rules_repo.create(rules)
        assert stored.rules_id is not None
        first = purchases.create(
            product_id=product.id,
            rules_id=stored.rules_id,
            mode=PurchaseMode.ASSISTED,
            test_mode=True,
        )
        purchases.transition(first.id, PurchaseState.BLOCKED, reason="over limit")
        second = purchases.create(
            product_id=product.id,
            rules_id=stored.rules_id,
            mode=PurchaseMode.ASSISTED,
            test_mode=True,
        )
        assert second.id != first.id


class TestCrashRecovery:
    def _job_in(self, products, rules_repo, purchases, snapshot, rules, state):
        product = products.upsert_from_snapshot(snapshot)
        stored = rules_repo.create(rules)
        assert stored.rules_id is not None
        job = purchases.create(
            product_id=product.id,
            rules_id=stored.rules_id,
            mode=PurchaseMode.AUTOMATIC,
            test_mode=False,
        )
        for step in (
            PurchaseState.PRODUCT_CHECK,
            PurchaseState.RULE_VALIDATION,
            PurchaseState.CART_PREPARATION,
            PurchaseState.CHECKOUT,
            PurchaseState.FINAL_VALIDATION,
            PurchaseState.SUBMITTING,
            PurchaseState.CONFIRMING,
        ):
            job = purchases.transition(job.id, step)
            if step is state:
                break
        return job

    def test_interrupted_submission_becomes_uncertain_not_retried(
        self, products, rules_repo, purchases, snapshot, rules
    ) -> None:
        job = self._job_in(
            products, rules_repo, purchases, snapshot, rules, PurchaseState.SUBMITTING
        )
        assert job.state is PurchaseState.SUBMITTING

        uncertain = purchases.recover_interrupted()
        assert uncertain == [job.id]
        recovered = purchases.get(job.id)
        assert recovered is not None
        assert recovered.state is PurchaseState.UNKNOWN
        assert recovered.outcome_code == ErrorCode.ORDER_RESULT_UNCERTAIN.value

    def test_interrupted_confirming_becomes_uncertain(
        self, products, rules_repo, purchases, snapshot, rules
    ) -> None:
        job = self._job_in(
            products, rules_repo, purchases, snapshot, rules, PurchaseState.CONFIRMING
        )
        assert purchases.recover_interrupted() == [job.id]
        recovered = purchases.get(job.id)
        assert recovered is not None and recovered.state is PurchaseState.UNKNOWN

    def test_interrupted_before_submission_fails_cleanly(
        self, products, rules_repo, purchases, snapshot, rules
    ) -> None:
        product = products.upsert_from_snapshot(snapshot)
        stored = rules_repo.create(rules)
        assert stored.rules_id is not None
        job = purchases.create(
            product_id=product.id,
            rules_id=stored.rules_id,
            mode=PurchaseMode.ASSISTED,
            test_mode=False,
        )
        purchases.transition(job.id, PurchaseState.PRODUCT_CHECK)

        assert purchases.recover_interrupted() == []
        recovered = purchases.get(job.id)
        assert recovered is not None
        assert recovered.state is PurchaseState.FAILED
        assert "Nothing was ordered" in (recovered.outcome_detail or "")

    def test_recovery_frees_the_product_only_after_resolution(
        self, products, rules_repo, purchases, snapshot, rules
    ) -> None:
        job = self._job_in(
            products, rules_repo, purchases, snapshot, rules, PurchaseState.SUBMITTING
        )
        purchases.recover_interrupted()
        product_id = job.product_id
        stored = rules_repo.get(job.rules_id)
        assert stored is not None and stored.rules_id is not None

        with pytest.raises(AppError):
            purchases.create(
                product_id=product_id,
                rules_id=stored.rules_id,
                mode=PurchaseMode.AUTOMATIC,
                test_mode=False,
            )

        purchases.resolve_unknown(
            job.id, order_was_placed=False, note="Checked Amazon; no order"
        )
        again = purchases.create(
            product_id=product_id,
            rules_id=stored.rules_id,
            mode=PurchaseMode.AUTOMATIC,
            test_mode=False,
        )
        assert again.id != job.id

    def test_resolution_requires_the_uncertain_state(
        self, products, rules_repo, purchases, snapshot, rules
    ) -> None:
        product = products.upsert_from_snapshot(snapshot)
        stored = rules_repo.create(rules)
        assert stored.rules_id is not None
        job = purchases.create(
            product_id=product.id,
            rules_id=stored.rules_id,
            mode=PurchaseMode.ASSISTED,
            test_mode=True,
        )
        with pytest.raises(AppError):
            purchases.resolve_unknown(job.id, order_was_placed=True, note="n/a")

    def test_recovery_is_idempotent(
        self, products, rules_repo, purchases, snapshot, rules
    ) -> None:
        self._job_in(
            products, rules_repo, purchases, snapshot, rules, PurchaseState.SUBMITTING
        )
        first = purchases.recover_interrupted()
        second = purchases.recover_interrupted()
        assert first and second == []


class TestOrders:
    def test_record_and_dedupe(
        self, products, orders, snapshot, rules, checkout
    ) -> None:
        product = products.upsert_from_snapshot(snapshot)
        confirmation = OrderConfirmation(
            verified=True,
            order_number="112-1234567-7654321",
            order_total=usd("117.94"),
        )
        first = orders.record(
            purchase_job_id=None,
            product_id=product.id,
            rules=rules,
            checkout=checkout,
            confirmation=confirmation,
            seller="Amazon.com",
            condition=ItemCondition.NEW,
        )
        assert first.total == usd("117.94")
        assert first.quantity == 1
        assert first.payment_label == "Visa ending 1234"

        again = orders.record(
            purchase_job_id=None,
            product_id=product.id,
            rules=rules,
            checkout=checkout,
            confirmation=confirmation,
            seller="Amazon.com",
            condition=ItemCondition.NEW,
        )
        assert again.id == first.id
        assert len(orders.list_recent()) == 1

    def test_order_url(self, products, orders, snapshot, rules, checkout) -> None:
        product = products.upsert_from_snapshot(snapshot)
        record = orders.record(
            purchase_job_id=None,
            product_id=product.id,
            rules=rules,
            checkout=checkout,
            confirmation=OrderConfirmation(
                verified=True, order_number="112-1234567-7654321"
            ),
            seller="Amazon.com",
            condition=ItemCondition.NEW,
        )
        assert record.order_url is not None
        assert "112-1234567-7654321" in record.order_url

    def test_record_without_a_total_is_refused(
        self, products, orders, snapshot, rules, checkout
    ) -> None:
        from dataclasses import replace

        product = products.upsert_from_snapshot(snapshot)
        with pytest.raises(AppError):
            orders.record(
                purchase_job_id=None,
                product_id=product.id,
                rules=rules,
                checkout=replace(checkout, order_total=None),
                confirmation=OrderConfirmation(verified=True, order_total=None),
                seller="Amazon.com",
                condition=ItemCondition.NEW,
            )

    def test_user_confirmed_order(
        self, products, orders, purchases, rules_repo, snapshot, rules
    ) -> None:
        product = products.upsert_from_snapshot(snapshot)
        stored = rules_repo.create(rules)
        assert stored.rules_id is not None
        job = purchases.create(
            product_id=product.id,
            rules_id=stored.rules_id,
            mode=PurchaseMode.ASSISTED,
            test_mode=False,
        )
        record = orders.record_user_confirmed(
            purchase_job_id=job.id,
            product_id=product.id,
            total=usd("117.94"),
            quantity=1,
            order_number=None,
            note="Found it in my Amazon orders",
        )
        assert record.confirmation_status is ConfirmationStatus.CONFIRMED_BY_USER
        assert record.purchase_job_id == job.id

    def test_total_spent(self, products, orders, snapshot, rules, checkout) -> None:
        product = products.upsert_from_snapshot(snapshot)
        for number in ("112-1111111-1111111", "112-2222222-2222222"):
            orders.record(
                purchase_job_id=None,
                product_id=product.id,
                rules=rules,
                checkout=checkout,
                confirmation=OrderConfirmation(verified=True, order_number=number),
                seller="Amazon.com",
                condition=ItemCondition.NEW,
            )
        total = orders.total_spent()
        assert total == usd("235.88")


class TestActivity:
    def test_add_and_filter(self, activity: ActivityRepository) -> None:
        activity.add(
            category=ActivityCategory.PURCHASE,
            severity=ActivitySeverity.SUCCESS,
            title="Purchase completed",
            amount=Money.from_decimal("117.94"),
        )
        activity.add(
            category=ActivityCategory.WATCH_CHECK,
            severity=ActivitySeverity.INFO,
            title="Checked",
        )
        assert len(activity.list_events()) == 2
        purchases_only = activity.list_events(category=ActivityCategory.PURCHASE)
        assert len(purchases_only) == 1
        assert purchases_only[0].amount == Money.from_decimal("117.94")

    def test_metadata_is_redacted_before_storage(
        self, activity: ActivityRepository
    ) -> None:
        activity.add(
            category=ActivityCategory.ERROR,
            severity=ActivitySeverity.ERROR,
            title="Failed",
            metadata={"session-token": "Atza|secret", "asin": ASIN},
        )
        event = activity.list_events()[0]
        assert event.metadata["asin"] == ASIN
        assert "secret" not in repr(event.metadata)

    def test_pruning(self, activity: ActivityRepository, monkeypatch) -> None:
        import app.database.repositories.activity as module

        monkeypatch.setattr(module, "ACTIVITY_RETENTION", 10)
        for index in range(25):
            activity.add(
                category=ActivityCategory.SYSTEM,
                severity=ActivitySeverity.INFO,
                title=f"Event {index}",
            )
        activity.prune()
        assert activity.count() == 10

    def test_notification_dedupe_window(self, activity: ActivityRepository) -> None:
        assert not activity.was_notified_recently("target:1", within_seconds=3600)
        activity.record_notification(
            kind="target_price_reached",
            dedupe_key="target:1",
            title="Target reached",
            body=None,
            delivered=True,
        )
        assert activity.was_notified_recently("target:1", within_seconds=3600)
        assert not activity.was_notified_recently("target:2", within_seconds=3600)

    def test_suppressed_notification_does_not_count(
        self, activity: ActivityRepository
    ) -> None:
        activity.record_notification(
            kind="back_in_stock",
            dedupe_key="stock:1",
            title="Back in stock",
            body=None,
            delivered=False,
            suppressed_why="turned off in settings",
        )
        assert not activity.was_notified_recently("stock:1", within_seconds=3600)

    def test_diagnostic_url_is_redacted(self, activity: ActivityRepository) -> None:
        activity.record_diagnostic(
            step="read_price",
            error_code="unexpected_page",
            page_url="https://www.amazon.com/dp/B07XYZ1234?session-id=secret",
            screenshot_file="shot.png",
            metadata={"cookies": "at-main=secret"},
        )
        stored = activity.list_diagnostics()[0]
        assert "secret" not in str(stored["page_url"])
        assert "secret" not in str(stored["metadata_json"])

    def test_clear_diagnostics_returns_filenames(
        self, activity: ActivityRepository
    ) -> None:
        activity.record_diagnostic(
            step="s", error_code=None, page_url=None, screenshot_file="a.png"
        )
        activity.record_diagnostic(
            step="s", error_code=None, page_url=None, screenshot_file="b.png"
        )
        files = activity.clear_diagnostics()
        assert sorted(files) == ["a.png", "b.png"]
        assert activity.list_diagnostics() == []


class TestRuleBrandIsFrozen:
    """The manufacturer seller policy must not follow a changed byline.

    ``PurchaseRules.brand`` is what ``amazon_or_manufacturer`` compares a
    seller name to. It used to be re-derived from ``products.brand`` on every
    check, and that row is refreshed from the product page's byline -- so a
    listing whose byline and seller name both change (what a listing takeover
    looks like) would satisfy "Amazon or the manufacturer" for whatever the
    byline then claimed. It is now stored on the rule row.
    """

    def test_the_brand_survives_a_round_trip(
        self, rules_repo: RulesRepository, rules: PurchaseRules
    ) -> None:
        stored = rules_repo.create(replace(rules, brand="Klein Tools"))
        assert stored.brand == "Klein Tools"
        assert stored.rules_id is not None
        assert rules_repo.get(stored.rules_id).brand == "Klein Tools"

    def test_a_later_product_brand_change_does_not_reach_the_rule(
        self,
        rules_repo: RulesRepository,
        products: ProductRepository,
        rules: PurchaseRules,
        snapshot: ProductSnapshot,
    ) -> None:
        record = products.upsert_from_snapshot(
            replace(snapshot, brand="Klein Tools")
        )
        assert record.brand == "Klein Tools"
        stored = rules_repo.create(replace(rules, brand=record.brand))

        # The page now claims a different manufacturer.
        refreshed = products.upsert_from_snapshot(
            replace(snapshot, brand="Bargain Bin Electronics")
        )
        assert refreshed.brand == "Bargain Bin Electronics"

        assert stored.rules_id is not None
        reloaded = rules_repo.get(stored.rules_id)
        assert reloaded.brand == "Klein Tools"
        assert not reloaded.seller_allowed("Bargain Bin Electronics")

    def test_an_updated_rule_keeps_its_brand(
        self, rules_repo: RulesRepository, rules: PurchaseRules
    ) -> None:
        stored = rules_repo.create(replace(rules, brand="Klein Tools"))
        assert stored.rules_id is not None
        updated = rules_repo.update(
            stored.rules_id, replace(stored, quantity=2)
        )
        assert updated.brand == "Klein Tools"
        assert updated.quantity == 2

    def test_a_duplicated_rule_keeps_its_brand(
        self, rules_repo: RulesRepository, rules: PurchaseRules
    ) -> None:
        stored = rules_repo.create(replace(rules, brand="Klein Tools"))
        assert stored.rules_id is not None
        copy = rules_repo.duplicate(stored.rules_id)
        assert copy.brand == "Klein Tools"
        assert copy.rules_id != stored.rules_id
