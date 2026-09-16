"""Tests for the watch-check decision logic and the scheduler's timing."""

from __future__ import annotations

import pytest

from app.config import NotificationKind
from app.core.money import Money
from app.database.records import PriceObservation, WatchAction, WatchStatus
from app.monitoring.scheduler import ClockJumpDetector, RequestPacer
from app.monitoring.watcher import evaluate_check
from app.purchasing.models import (
    Availability,
    ConditionPolicy,
    ItemCondition,
    ProductSnapshot,
    PurchaseRules,
    SellerPolicy,
    VariationSnapshot,
)

ASIN = "B07XYZ1234"


def usd(amount: str) -> Money:
    return Money.from_decimal(amount, "USD")


@pytest.fixture
def rules() -> PurchaseRules:
    return PurchaseRules(
        expected_asin=ASIN,
        max_item_price=usd("100.00"),
        max_order_total=usd("115.00"),
        seller_policy=SellerPolicy.AMAZON_ONLY,
        condition_policy=ConditionPolicy.NEW_ONLY,
        expected_variation=VariationSnapshot({"Color": "Black"}),
        expected_seller="Amazon.com",
    )


def snapshot(**overrides: object) -> ProductSnapshot:
    defaults: dict[str, object] = {
        "asin": ASIN,
        "title": "Clamp meter",
        "price": usd("109.97"),
        "availability": Availability.IN_STOCK,
        "availability_text": "In Stock",
        "seller": "Amazon.com",
        "ships_from": "Amazon.com",
        "condition": ItemCondition.NEW,
        "variation": VariationSnapshot({"Color": "Black"}),
        "max_quantity": 30,
    }
    defaults.update(overrides)
    return ProductSnapshot(**defaults)  # type: ignore[arg-type]


def observation(**overrides: object) -> PriceObservation:
    defaults: dict[str, object] = {
        "id": 1,
        "product_id": 1,
        "observed_at": None,
        "price": usd("109.97"),
        "in_stock": True,
        "availability": Availability.IN_STOCK,
        "availability_text": "In Stock",
        "seller": "Amazon.com",
        "ships_from": "Amazon.com",
        "condition": ItemCondition.NEW,
        "variation_fingerprint": None,
    }
    defaults.update(overrides)
    return PriceObservation(**defaults)  # type: ignore[arg-type]


def decide(rules: PurchaseRules, **kwargs: object):
    params: dict[str, object] = {
        "rules": rules,
        "snapshot": snapshot(),
        "previous": None,
        "action": WatchAction.NOTIFY,
        "trigger_in_stock": True,
        "trigger_target_price": True,
    }
    params.update(kwargs)
    return evaluate_check(**params)  # type: ignore[arg-type]


class TestStatus:
    def test_above_target_is_waiting_for_price(self, rules) -> None:
        decision = decide(rules, snapshot=snapshot(price=usd("109.97")))
        assert decision.status is WatchStatus.WAITING_FOR_PRICE
        assert not decision.target_reached
        assert "waiting for $100.00" in decision.summary

    def test_at_target_is_reached(self, rules) -> None:
        decision = decide(rules, snapshot=snapshot(price=usd("100.00")))
        assert decision.status is WatchStatus.TARGET_REACHED
        assert decision.target_reached

    def test_below_target_is_reached(self, rules) -> None:
        decision = decide(rules, snapshot=snapshot(price=usd("94.50")))
        assert decision.target_reached
        assert "at or below your target" in decision.summary

    def test_out_of_stock(self, rules) -> None:
        decision = decide(
            rules,
            snapshot=snapshot(
                availability=Availability.OUT_OF_STOCK,
                availability_text="Currently unavailable",
                price=None,
            ),
        )
        assert decision.status is WatchStatus.OUT_OF_STOCK
        assert not decision.target_reached
        assert decision.summary == "Currently unavailable"

    def test_no_target_means_plain_watching(self) -> None:
        loose = PurchaseRules(expected_asin=ASIN, seller_policy=SellerPolicy.ANY)
        decision = decide(loose)
        assert decision.status is WatchStatus.WATCHING
        assert decision.target_reached

    def test_unreadable_price_never_meets_a_target(self, rules) -> None:
        """A missing price must not be treated as a bargain."""
        decision = decide(rules, snapshot=snapshot(price=None))
        assert not decision.target_reached
        assert not decision.should_buy

    def test_different_currency_never_meets_a_target(self, rules) -> None:
        decision = decide(
            rules, snapshot=snapshot(price=Money.from_decimal("50.00", "EUR"))
        )
        assert not decision.target_reached


class TestTransitions:
    def test_back_in_stock(self, rules) -> None:
        decision = decide(rules, previous=observation(in_stock=False))
        assert decision.back_in_stock
        assert NotificationKind.BACK_IN_STOCK in decision.notifications

    def test_still_in_stock_is_not_back_in_stock(self, rules) -> None:
        decision = decide(rules, previous=observation(in_stock=True))
        assert not decision.back_in_stock

    def test_no_history_is_not_back_in_stock(self, rules) -> None:
        assert not decide(rules, previous=None).back_in_stock

    def test_price_drop_is_noticed(self, rules) -> None:
        decision = decide(
            rules,
            snapshot=snapshot(price=usd("99.00")),
            previous=observation(price=usd("109.97")),
        )
        assert decision.price_dropped

    def test_price_rise_is_not_a_drop(self, rules) -> None:
        decision = decide(
            rules,
            snapshot=snapshot(price=usd("119.00")),
            previous=observation(price=usd("109.97")),
        )
        assert not decision.price_dropped


class TestSellerChange:
    def test_changed_seller_is_flagged_and_notified(self, rules) -> None:
        decision = decide(rules, snapshot=snapshot(seller="XYZ Marketplace LLC"))
        assert decision.seller_changed
        assert NotificationKind.SELLER_CHANGED in decision.notifications

    def test_same_seller_is_not_flagged(self, rules) -> None:
        assert not decide(rules).seller_changed

    def test_unreadable_seller_is_flagged(self, rules) -> None:
        """It will block a purchase, so the user should hear about it."""
        assert decide(rules, snapshot=snapshot(seller=None)).seller_changed

    def test_no_recorded_seller_means_no_comparison(self) -> None:
        from dataclasses import replace

        loose = replace(
            PurchaseRules(expected_asin=ASIN, seller_policy=SellerPolicy.ANY),
            expected_seller=None,
        )
        assert not decide(loose, snapshot=snapshot(seller="Anyone")).seller_changed


class TestShouldBuy:
    def test_notify_action_never_buys(self, rules) -> None:
        decision = decide(
            rules,
            snapshot=snapshot(price=usd("94.50")),
            action=WatchAction.NOTIFY,
        )
        assert decision.target_reached
        assert not decision.should_buy

    def test_buy_action_at_target_buys(self, rules) -> None:
        decision = decide(
            rules, snapshot=snapshot(price=usd("94.50")), action=WatchAction.BUY
        )
        assert decision.should_buy

    def test_buy_action_above_target_does_not_buy(self, rules) -> None:
        decision = decide(
            rules, snapshot=snapshot(price=usd("109.97")), action=WatchAction.BUY
        )
        assert not decision.should_buy

    def test_out_of_stock_never_buys(self, rules) -> None:
        decision = decide(
            rules,
            snapshot=snapshot(
                availability=Availability.OUT_OF_STOCK, price=usd("50.00")
            ),
            action=WatchAction.BUY,
        )
        assert not decision.should_buy

    def test_a_failing_guard_prevents_buying(self, rules) -> None:
        """The same guard the purchase uses also gates the trigger."""
        decision = decide(
            rules,
            snapshot=snapshot(price=usd("50.00"), seller="XYZ Marketplace LLC"),
            action=WatchAction.BUY,
        )
        assert not decision.should_buy
        assert not decision.rules_satisfied

    def test_wrong_variation_prevents_buying(self, rules) -> None:
        decision = decide(
            rules,
            snapshot=snapshot(
                price=usd("50.00"), variation=VariationSnapshot({"Color": "Red"})
            ),
            action=WatchAction.BUY,
        )
        assert not decision.should_buy

    def test_used_condition_prevents_buying(self, rules) -> None:
        decision = decide(
            rules,
            snapshot=snapshot(price=usd("50.00"), condition=ItemCondition.USED),
            action=WatchAction.BUY,
        )
        assert not decision.should_buy

    def test_in_stock_only_trigger(self, rules) -> None:
        """With the price trigger off, coming into stock is enough to buy.

        The maximum item price still has to be satisfied -- see the test
        below. This case is within it.
        """
        decision = decide(
            rules,
            snapshot=snapshot(price=usd("94.50")),
            action=WatchAction.BUY,
            trigger_target_price=False,
        )
        assert decision.should_buy

    def test_disabling_the_price_trigger_does_not_bypass_the_price_limit(
        self, rules
    ) -> None:
        """A trigger decides *when* to buy; a limit decides *whether*.

        Turning the price trigger off must never permit a purchase above the
        maximum item price the user set.
        """
        decision = decide(
            rules,
            snapshot=snapshot(price=usd("109.97")),
            action=WatchAction.BUY,
            trigger_target_price=False,
            trigger_in_stock=True,
        )
        assert not decision.should_buy
        assert not decision.rules_satisfied

    def test_price_only_trigger_still_requires_stock(self, rules) -> None:
        decision = decide(
            rules,
            snapshot=snapshot(
                availability=Availability.OUT_OF_STOCK, price=usd("50.00")
            ),
            action=WatchAction.BUY,
            trigger_in_stock=False,
        )
        assert not decision.should_buy

    def test_no_notification_for_an_auto_buy_target(self, rules) -> None:
        """An automatic watch reports the purchase, not the price."""
        decision = decide(
            rules, snapshot=snapshot(price=usd("94.50")), action=WatchAction.BUY
        )
        assert NotificationKind.TARGET_PRICE_REACHED not in decision.notifications


class TestRequestPacer:
    def test_first_request_is_allowed(self) -> None:
        pacer = RequestPacer(minimum_gap_seconds=15)
        assert pacer.check(now=100.0).allowed

    def test_a_second_request_too_soon_is_refused(self) -> None:
        pacer = RequestPacer(minimum_gap_seconds=15)
        pacer.record_request(now=100.0)
        decision = pacer.check(now=105.0)
        assert not decision.allowed
        assert decision.wait_seconds == pytest.approx(10.0)
        assert "polite" in decision.reason

    def test_a_request_after_the_gap_is_allowed(self) -> None:
        pacer = RequestPacer(minimum_gap_seconds=15)
        pacer.record_request(now=100.0)
        assert pacer.check(now=116.0).allowed

    def test_reset_clears_the_gap(self) -> None:
        pacer = RequestPacer(minimum_gap_seconds=15)
        pacer.record_request(now=100.0)
        pacer.reset()
        assert pacer.check(now=101.0).allowed

    def test_the_gap_does_not_grow_with_more_jobs(self) -> None:
        """Pacing is global, so more watches do not mean more requests."""
        pacer = RequestPacer(minimum_gap_seconds=15)
        allowed = 0
        now = 0.0
        for _ in range(50):
            if pacer.check(now=now).allowed:
                pacer.record_request(now=now)
                allowed += 1
            now += 1.0
        # 50 seconds at a 15-second floor permits 4 requests.
        assert allowed == 4


class TestClockJumpDetector:
    def test_normal_tick_is_not_a_jump(self) -> None:
        detector = ClockJumpDetector(interval_seconds=10, threshold_seconds=90)
        detector.evaluate(monotonic_now=1_000.0, wall_now=5_000.0)
        assert detector.evaluate(monotonic_now=1_010.0, wall_now=5_010.0) is None

    def test_wall_clock_jump_is_detected(self) -> None:
        """Modern standby advances the wall clock but not the monotonic one."""
        detector = ClockJumpDetector(interval_seconds=10, threshold_seconds=90)
        detector.evaluate(monotonic_now=1_000.0, wall_now=5_000.0)
        jump = detector.evaluate(monotonic_now=1_010.0, wall_now=8_600.0)
        assert jump is not None
        assert jump == pytest.approx(3_590.0)

    def test_monotonic_jump_is_detected(self) -> None:
        detector = ClockJumpDetector(interval_seconds=10, threshold_seconds=90)
        detector.evaluate(monotonic_now=1_000.0, wall_now=5_000.0)
        jump = detector.evaluate(monotonic_now=4_600.0, wall_now=5_010.0)
        assert jump is not None
        assert jump == pytest.approx(3_590.0)

    def test_small_drift_is_ignored(self) -> None:
        detector = ClockJumpDetector(interval_seconds=10, threshold_seconds=90)
        detector.evaluate(monotonic_now=1_000.0, wall_now=5_000.0)
        assert detector.evaluate(monotonic_now=1_070.0, wall_now=5_070.0) is None
