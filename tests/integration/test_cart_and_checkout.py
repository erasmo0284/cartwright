"""Cart and checkout tests against real Chromium and fixture pages.

The tests in ``TestSubmitBarriers`` are the most important in the suite: they
assert that the only code path capable of placing an order refuses to do so
in test mode, without a passing guard, or when the total has moved.
"""

from __future__ import annotations

import pytest

from app.automation.cart_manager import MANAGER as CART
from app.automation.cart_manager import IsolationJournal
from app.automation.checkout_manager import MANAGER as CHECKOUT
from app.automation.checkout_manager import SubmitAuthorization
from app.core.errors import AppError, ErrorCode
from app.core.money import Money
from app.purchasing.models import (
    Availability,
    CartStrategy,
    ItemCondition,
    ProductSnapshot,
    PurchaseRules,
    VariationSnapshot,
)
from app.purchasing.purchase_guard import GUARD
from tests.fixtures import amazon_pages as pages

pytestmark = pytest.mark.integration

ASIN = pages.DEFAULT_ASIN
CART_URL = "https://www.amazon.com/gp/cart/view.html"
CHECKOUT_URL = "https://www.amazon.com/gp/buy/spc/handlers/display.html"


def usd(amount: str) -> Money:
    return Money.from_decimal(amount, "USD")


@pytest.fixture
def rules() -> PurchaseRules:
    return PurchaseRules(
        expected_asin=ASIN,
        quantity=1,
        max_item_price=usd("120.00"),
        max_order_total=usd("135.00"),
        expected_variation=VariationSnapshot({"Color": "Black"}),
        expected_seller="Amazon.com",
        expected_address_label="John D., Raleigh, NC 27601",
        expected_payment_label="Visa ending in 1234",
    )


@pytest.fixture
def product() -> ProductSnapshot:
    return ProductSnapshot(
        asin=ASIN,
        title=pages.DEFAULT_TITLE,
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


class TestReadCart:
    def test_empty_cart(self, load) -> None:
        reader = load(CART_URL, pages.cart_page())
        cart = CART.read_cart(reader)
        assert cart.is_empty
        assert cart.reported_empty
        assert cart.lines == ()
        assert cart.subtotal is None

    def test_empty_cart_ignores_the_recommendations_strip(self, load) -> None:
        """A page-wide data-asin scrape would return four recommended items."""
        reader = load(CART_URL, pages.cart_page())
        cart = CART.read_cart(reader)
        assert len(cart.lines) == 0

    def test_single_target_item(self, load) -> None:
        reader = load(CART_URL, pages.cart_with_target_only())
        cart = CART.read_cart(reader)
        assert not cart.is_empty
        assert len(cart.lines) == 1
        line = cart.lines[0]
        assert line.asin == ASIN
        assert line.quantity == 1
        assert line.unit_price == usd("109.97")
        assert cart.subtotal == usd("109.97")
        assert cart.foreign_lines(ASIN) == ()

    def test_quantity_is_read_from_the_control(self, load) -> None:
        reader = load(CART_URL, pages.cart_with_target_only(quantity=3))
        cart = CART.read_cart(reader)
        assert cart.lines[0].quantity == 3
        assert cart.total_units == 3

    def test_cart_with_unrelated_items(self, load) -> None:
        reader = load(CART_URL, pages.cart_with_other_items())
        cart = CART.read_cart(reader)
        assert len(cart.lines) == 3
        foreign = cart.foreign_lines(ASIN)
        assert len(foreign) == 2
        assert {line.asin for line in foreign} == {"B0DOGFOOD01", "B0KETTLE001"}

    def test_saved_for_later_is_counted_but_not_a_cart_line(self, load) -> None:
        reader = load(
            CART_URL,
            pages.cart_page(
                lines=[
                    {
                        "asin": ASIN,
                        "title": pages.DEFAULT_TITLE,
                        "quantity": 1,
                        "price": "109.97",
                        "row_id": "t",
                    }
                ],
                subtotal="109.97",
                saved_for_later=[
                    {"asin": "B0SAVED0001", "title": "Saved thing"},
                    {"asin": "B0SAVED0002", "title": "Another saved thing"},
                ],
            ),
        )
        cart = CART.read_cart(reader)
        assert len(cart.lines) == 1
        assert cart.saved_for_later_count == 2


class TestCartIsolationPlan:
    def test_buy_now_is_preferred_even_with_a_full_cart(
        self, load, product, rules
    ) -> None:
        reader = load(CART_URL, pages.cart_with_other_items())
        cart = CART.read_cart(reader)
        plan = CART.plan_isolation(
            product=product, rules=rules, cart=cart, allow_set_aside=False
        )
        assert plan.strategy is CartStrategy.BUY_NOW
        assert not plan.needs_permission

    def test_empty_cart_route(self, load, product, rules) -> None:
        from dataclasses import replace

        reader = load(CART_URL, pages.cart_page())
        cart = CART.read_cart(reader)
        plan = CART.plan_isolation(
            product=replace(product, buy_now_available=False),
            rules=rules,
            cart=cart,
            allow_set_aside=False,
        )
        assert plan.strategy is CartStrategy.EMPTY_CART

    def test_full_cart_without_buy_now_is_blocked_by_default(
        self, load, product, rules
    ) -> None:
        """The default must never be to check out someone's whole cart."""
        from dataclasses import replace

        reader = load(CART_URL, pages.cart_with_other_items())
        cart = CART.read_cart(reader)
        plan = CART.plan_isolation(
            product=replace(product, buy_now_available=False),
            rules=rules,
            cart=cart,
            allow_set_aside=False,
        )
        assert plan.strategy is CartStrategy.BLOCKED
        assert plan.is_blocked
        assert len(plan.foreign_lines) == 2
        assert "other item" in plan.describe()

    def test_set_aside_requires_permission(self, load, product, rules) -> None:
        from dataclasses import replace

        reader = load(CART_URL, pages.cart_with_other_items())
        cart = CART.read_cart(reader)
        plan = CART.plan_isolation(
            product=replace(product, buy_now_available=False),
            rules=rules,
            cart=cart,
            allow_set_aside=True,
        )
        assert plan.strategy is CartStrategy.SET_ASIDE_OTHERS
        assert plan.needs_permission

    def test_no_purchase_control_at_all_is_blocked(
        self, load, product, rules
    ) -> None:
        from dataclasses import replace

        reader = load(CART_URL, pages.cart_with_other_items())
        cart = CART.read_cart(reader)
        plan = CART.plan_isolation(
            product=replace(
                product, buy_now_available=False, add_to_cart_available=False
            ),
            rules=rules,
            cart=cart,
            allow_set_aside=True,
        )
        assert plan.strategy is CartStrategy.BLOCKED


class TestGuardAgainstRealCart:
    def test_polluted_cart_blocks_the_purchase(self, load, product, rules) -> None:
        reader = load(CART_URL, pages.cart_with_other_items())
        cart = CART.read_cart(reader)
        report = GUARD.check_cart(rules, product, cart)
        assert not report.passed
        assert report.blocked_code is ErrorCode.UNEXPECTED_CART_ITEMS

    def test_clean_cart_passes(self, load, product, rules) -> None:
        reader = load(CART_URL, pages.cart_with_target_only())
        cart = CART.read_cart(reader)
        report = GUARD.check_cart(rules, product, cart)
        assert report.passed, [c.title for c in report.blocking_failures]


class TestIsolationJournal:
    def test_round_trip(self) -> None:
        journal = IsolationJournal()
        journal.set_aside.append({"asin": "B0DOGFOOD01", "quantity": 2})
        restored = IsolationJournal.from_json(journal.to_json())
        assert restored.set_aside == journal.set_aside
        assert restored.has_pending_restore

    def test_restored_journal_has_nothing_pending(self) -> None:
        journal = IsolationJournal()
        journal.set_aside.append({"asin": "B0DOGFOOD01"})
        journal.restored = True
        assert not journal.has_pending_restore

    def test_malformed_json_is_tolerated(self) -> None:
        assert not IsolationJournal.from_json("not json").has_pending_restore
        assert not IsolationJournal.from_json(None).has_pending_restore


class TestReadCheckout:
    def test_reads_the_full_summary(self, load) -> None:
        reader = load(CHECKOUT_URL, pages.checkout_page())
        snapshot = CHECKOUT.read_checkout(reader)

        assert snapshot.item_subtotal == usd("109.97")
        assert snapshot.shipping == usd("0.00")
        assert snapshot.tax == usd("7.97")
        assert snapshot.order_total == usd("117.94")
        assert snapshot.address_label is not None
        assert "Raleigh" in snapshot.address_label
        assert snapshot.payment_label == "Visa ending in 1234"
        assert snapshot.place_order_control_found is True
        assert snapshot.addons == ()
        assert snapshot.is_subscription is False

    def test_order_total_is_not_confused_with_the_tax_row(self, load) -> None:
        """"Estimated tax to be collected" must not be read as the total."""
        reader = load(
            CHECKOUT_URL,
            pages.checkout_page(tax="19.00", order_total="128.97"),
        )
        snapshot = CHECKOUT.read_checkout(reader)
        assert snapshot.tax == usd("19.00")
        assert snapshot.order_total == usd("128.97")

    def test_line_item_quantity_and_asin(self, load) -> None:
        reader = load(CHECKOUT_URL, pages.checkout_page(quantity=2))
        snapshot = CHECKOUT.read_checkout(reader)
        matching = snapshot.lines_for(ASIN)
        assert matching
        assert sum(line.quantity for line in matching) == 2

    def test_missing_address_is_none(self, load) -> None:
        reader = load(CHECKOUT_URL, pages.checkout_page(address=None))
        snapshot = CHECKOUT.read_checkout(reader)
        assert not snapshot.address_label

    def test_missing_payment_is_none(self, load) -> None:
        reader = load(CHECKOUT_URL, pages.checkout_page(payment=None))
        snapshot = CHECKOUT.read_checkout(reader)
        assert not snapshot.payment_label

    def test_addon_line_is_detected(self, load) -> None:
        reader = load(
            CHECKOUT_URL,
            pages.checkout_page(addon="3-Year Protection Plan", order_total="142.93"),
        )
        snapshot = CHECKOUT.read_checkout(reader)
        assert snapshot.addons
        assert "Protection Plan" in snapshot.addons[0]

    def test_subscription_at_checkout_is_detected(self, load) -> None:
        reader = load(CHECKOUT_URL, pages.checkout_page(subscription=True))
        snapshot = CHECKOUT.read_checkout(reader)
        assert snapshot.is_subscription is True

    def test_missing_order_button_is_reported(self, load) -> None:
        reader = load(CHECKOUT_URL, pages.checkout_page(place_order_button=False))
        snapshot = CHECKOUT.read_checkout(reader)
        assert snapshot.place_order_control_found is False

    def test_duplicate_order_buttons_are_counted(self, load) -> None:
        reader = load(CHECKOUT_URL, pages.checkout_page(duplicate_place_order=True))
        control = CHECKOUT.find_place_order(reader)
        assert control is not None
        assert control.matches >= 2


class TestGuardAgainstRealCheckout:
    def test_conforming_checkout_passes(self, load, product, rules) -> None:
        reader = load(CHECKOUT_URL, pages.checkout_page())
        snapshot = CHECKOUT.read_checkout(reader)
        report = GUARD.check_final(rules, product, snapshot)
        assert report.passed, [
            f"{c.title}: {c.expected} vs {c.actual}" for c in report.blocking_failures
        ]

    def test_over_limit_total_blocks(self, load, product, rules) -> None:
        reader = load(
            CHECKOUT_URL,
            pages.checkout_page(shipping="30.00", order_total="147.94"),
        )
        snapshot = CHECKOUT.read_checkout(reader)
        report = GUARD.check_final(rules, product, snapshot)
        assert not report.passed
        assert report.blocked_code is ErrorCode.TOTAL_ABOVE_LIMIT

    def test_addon_blocks(self, load, product, rules) -> None:
        reader = load(
            CHECKOUT_URL,
            pages.checkout_page(addon="3-Year Protection Plan", order_total="130.00"),
        )
        snapshot = CHECKOUT.read_checkout(reader)
        report = GUARD.check_final(rules, product, snapshot)
        assert not report.passed

    def test_wrong_payment_method_blocks(self, load, product, rules) -> None:
        reader = load(
            CHECKOUT_URL, pages.checkout_page(payment="Mastercard ending in 9999")
        )
        snapshot = CHECKOUT.read_checkout(reader)
        report = GUARD.check_final(rules, product, snapshot)
        assert not report.passed
        assert report.blocked_code is ErrorCode.PAYMENT_METHOD_PROBLEM


class TestSubmitBarriers:
    """The order button must not be clickable except under exact conditions."""

    def _authorization(self, **overrides: object) -> SubmitAuthorization:
        calls: list[str] = []
        defaults: dict[str, object] = {
            "purchase_job_id": 1,
            "attempt_id": 1,
            "approved_total": usd("117.94"),
            "guard_passed": True,
            "test_mode": False,
            "record_submission": lambda: calls.append("recorded"),
        }
        defaults.update(overrides)
        authorization = SubmitAuthorization(**defaults)  # type: ignore[arg-type]
        authorization.__dict__["_calls"] = calls
        return authorization

    def test_test_mode_cannot_submit(self, load) -> None:
        reader = load(CHECKOUT_URL, pages.checkout_page())
        authorization = self._authorization(test_mode=True)
        with pytest.raises(AppError) as excinfo:
            CHECKOUT.submit(reader, authorization)
        assert excinfo.value.code is ErrorCode.INTERNAL_ERROR
        assert "Test mode" in excinfo.value.detail
        assert authorization.__dict__["_calls"] == []

    def test_a_failed_guard_cannot_submit(self, load) -> None:
        reader = load(CHECKOUT_URL, pages.checkout_page())
        authorization = self._authorization(guard_passed=False)
        with pytest.raises(AppError):
            CHECKOUT.submit(reader, authorization)
        assert authorization.__dict__["_calls"] == []

    def test_a_changed_total_cannot_submit(self, load) -> None:
        """Consent was given for a specific amount."""
        reader = load(CHECKOUT_URL, pages.checkout_page(order_total="125.00"))
        authorization = self._authorization(approved_total=usd("117.94"))
        with pytest.raises(AppError) as excinfo:
            CHECKOUT.submit(reader, authorization)
        assert excinfo.value.code is ErrorCode.CHECKOUT_TOTAL_CHANGED
        assert authorization.__dict__["_calls"] == []

    def test_a_lower_total_also_cannot_submit(self, load) -> None:
        reader = load(CHECKOUT_URL, pages.checkout_page(order_total="99.00"))
        authorization = self._authorization(approved_total=usd("117.94"))
        with pytest.raises(AppError) as excinfo:
            CHECKOUT.submit(reader, authorization)
        assert excinfo.value.code is ErrorCode.CHECKOUT_TOTAL_CHANGED

    def test_unreadable_total_cannot_submit(self, load, page, site) -> None:
        html = pages.checkout_page().replace("Order total", "Total due later")
        site.add("gp/buy/spc", html)
        page.goto(CHECKOUT_URL, wait_until="domcontentloaded")
        from app.automation.page_reader import PageReader

        authorization = self._authorization()
        with pytest.raises(AppError) as excinfo:
            CHECKOUT.submit(PageReader(page), authorization)
        assert excinfo.value.code is ErrorCode.CHECKOUT_TOTAL_CHANGED

    def test_missing_order_button_cannot_submit(self, load) -> None:
        reader = load(CHECKOUT_URL, pages.checkout_page(place_order_button=False))
        authorization = self._authorization()
        with pytest.raises(AppError) as excinfo:
            CHECKOUT.submit(reader, authorization)
        assert excinfo.value.code is ErrorCode.CHECKOUT_CHANGED
        assert authorization.__dict__["_calls"] == []

    def test_submission_is_recorded_before_the_click(self, load, page, site) -> None:
        """A crash during the click must still leave evidence of a submission."""
        order: list[str] = []

        def record() -> None:
            order.append("recorded")

        site.add("gp/buy/spc", pages.checkout_page())
        site.add(
            "gp/buy/thankyou",
            pages.confirmation_page(),
        )
        page.goto(CHECKOUT_URL, wait_until="domcontentloaded")
        page.expose_function("__noteClick", lambda: order.append("clicked"))
        page.evaluate(
            """
            const button = document.querySelector("input[name='placeYourOrder1']");
            button.addEventListener('click', (event) => {
                event.preventDefault();
                window.__noteClick();
            });
            """
        )
        from app.automation.page_reader import PageReader

        authorization = SubmitAuthorization(
            purchase_job_id=1,
            attempt_id=1,
            approved_total=usd("117.94"),
            guard_passed=True,
            test_mode=False,
            record_submission=record,
        )
        CHECKOUT.submit(PageReader(page), authorization)
        assert order == ["recorded", "clicked"]

    def test_a_raising_recorder_prevents_the_click(self, load, page, site) -> None:
        """If the submission cannot be recorded, nothing is clicked."""
        clicks: list[str] = []
        site.add("gp/buy/spc", pages.checkout_page())
        page.goto(CHECKOUT_URL, wait_until="domcontentloaded")
        page.expose_function("__noteClick", lambda: clicks.append("clicked"))
        page.evaluate(
            """
            document.querySelector("input[name='placeYourOrder1']")
              .addEventListener('click', (e) => { e.preventDefault(); window.__noteClick(); });
            """
        )

        def record() -> None:
            raise AppError(ErrorCode.DUPLICATE_BLOCKED)

        from app.automation.page_reader import PageReader

        authorization = SubmitAuthorization(
            purchase_job_id=1,
            attempt_id=1,
            approved_total=usd("117.94"),
            guard_passed=True,
            test_mode=False,
            record_submission=record,
        )
        with pytest.raises(AppError) as excinfo:
            CHECKOUT.submit(PageReader(page), authorization)
        assert excinfo.value.code is ErrorCode.DUPLICATE_BLOCKED
        assert clicks == []


class TestConfirmation:
    def test_successful_confirmation(self, load) -> None:
        reader = load(
            "https://www.amazon.com/gp/buy/thankyou/handlers/display.html"
            "?purchaseId=112-1234567-7654321",
            pages.confirmation_page(),
        )
        confirmation = CHECKOUT.read_confirmation(reader)
        assert confirmation.verified is True
        assert confirmation.order_number == "112-1234567-7654321"

    def test_order_number_from_the_page_when_absent_from_the_url(self, load) -> None:
        reader = load(
            "https://www.amazon.com/gp/buy/thankyou/handlers/display.html",
            pages.confirmation_page(order_number="113-7654321-1234567"),
        )
        confirmation = CHECKOUT.read_confirmation(reader)
        assert confirmation.order_number == "113-7654321-1234567"

    def test_a_payment_error_is_not_a_confirmation(self, load) -> None:
        reader = load(CHECKOUT_URL, pages.confirmation_failed_page())
        confirmation = CHECKOUT.read_confirmation(reader)
        assert confirmation.verified is False
        assert confirmation.order_number is None

    def test_an_unrelated_page_is_not_a_confirmation(self, load) -> None:
        reader = load(CHECKOUT_URL, pages.service_error_page())
        confirmation = CHECKOUT.read_confirmation(reader)
        assert confirmation.verified is False

    def test_orders_page_verification(self, load) -> None:
        reader = load(
            "https://www.amazon.com/gp/css/order-history",
            pages.orders_history_page(),
        )
        confirmation = CHECKOUT.verify_via_orders_page(
            reader, expected_total=usd("117.94")
        )
        assert confirmation.verified is True
        assert confirmation.order_number == "112-1234567-7654321"

    def test_orders_page_rejects_a_different_total(self, load) -> None:
        reader = load(
            "https://www.amazon.com/gp/css/order-history",
            pages.orders_history_page(total="999.00"),
        )
        confirmation = CHECKOUT.verify_via_orders_page(
            reader, expected_total=usd("117.94")
        )
        assert confirmation.verified is False

    def test_orders_page_with_no_orders(self, load) -> None:
        reader = load(
            "https://www.amazon.com/gp/css/order-history",
            pages.cart_page(),
        )
        confirmation = CHECKOUT.verify_via_orders_page(reader)
        assert confirmation.verified is False


class TestAddonDecline:
    def test_addon_sheet_is_declined(self, load) -> None:
        reader = load(
            "https://www.amazon.com/dp/B07XYZ1234", pages.add_to_cart_addon_sheet()
        )
        offered = CART.decline_addons(reader)
        assert offered

    def test_no_sheet_means_nothing_to_decline(self, load) -> None:
        reader = load("https://www.amazon.com/dp/B07XYZ1234", pages.product_page())
        assert CART.decline_addons(reader) == ()
