"""Tests for the Purchase Guard.

Structured as an attack suite: each test tries to get a purchase authorised
that should not be. A failure here means real money could be spent wrongly.
"""

from __future__ import annotations

import pytest

from app.core.errors import ErrorCode
from app.core.money import Money
from app.purchasing.models import (
    Availability,
    CartLine,
    CartState,
    CheckoutSnapshot,
    ConditionPolicy,
    ItemCondition,
    ProductSnapshot,
    PurchaseRules,
    SellerPolicy,
    VariationSnapshot,
)
from app.purchasing.purchase_guard import (
    CHECK_ADDONS,
    CHECK_ADDRESS,
    CHECK_ASIN,
    CHECK_AVAILABILITY,
    CHECK_CART_CONTENTS,
    CHECK_CONDITION,
    CHECK_MAX_ITEM_PRICE,
    CHECK_MAX_ORDER_TOTAL,
    CHECK_PAYMENT,
    CHECK_PLACE_ORDER_CONTROL,
    CHECK_PRIME,
    CHECK_QUANTITY,
    CHECK_SELLER,
    CHECK_SELLER_CHANGED,
    CHECK_SUBSCRIPTION,
    CHECK_VARIATION,
    GUARD,
    verify_total_unchanged,
)
from app.purchasing.validation import CheckStatus

ASIN = "B07XYZ1234"


def usd(amount: str) -> Money:
    return Money.from_decimal(amount, "USD")


@pytest.fixture
def rules() -> PurchaseRules:
    """A conservative, fully configured rule set."""
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
        expected_payment_label="Visa ending 1234",
    )


@pytest.fixture
def product() -> ProductSnapshot:
    """A product that satisfies the fixture rules."""
    return ProductSnapshot(
        asin=ASIN,
        title="Klein Tools CL800 Clamp Meter",
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
def cart() -> CartState:
    return CartState(
        lines=(
            CartLine(
                asin=ASIN,
                title="Klein Tools CL800 Clamp Meter",
                quantity=1,
                unit_price=usd("109.97"),
                line_price=usd("109.97"),
            ),
        ),
        subtotal=usd("109.97"),
    )


@pytest.fixture
def checkout() -> CheckoutSnapshot:
    return CheckoutSnapshot(
        lines=(
            CartLine(
                asin=ASIN,
                title="Klein Tools CL800 Clamp Meter",
                quantity=1,
                unit_price=usd("109.97"),
                line_price=usd("109.97"),
            ),
        ),
        item_subtotal=usd("109.97"),
        shipping=usd("0.00"),
        tax=usd("7.97"),
        order_total=usd("117.94"),
        address_label="John D., Raleigh, NC",
        payment_label="Visa ending 1234",
        place_order_control_found=True,
    )


def status_of(report, check_id: str) -> CheckStatus:
    check = report.find(check_id)
    assert check is not None, f"no check {check_id}"
    return check.status


class TestHappyPath:
    def test_conforming_product_passes(self, rules, product) -> None:
        report = GUARD.check_product(rules, product)
        assert report.passed, [c.title for c in report.blocking_failures]

    def test_conforming_cart_passes(self, rules, product, cart) -> None:
        assert GUARD.check_cart(rules, product, cart).passed

    def test_conforming_checkout_passes(self, rules, product, checkout) -> None:
        report = GUARD.check_final(rules, product, checkout)
        assert report.passed, [c.title for c in report.blocking_failures]
        assert report.summary == "All checks passed"
        assert report.blocked_code is None

    def test_every_check_has_a_human_title(self, rules, product, checkout) -> None:
        for report in (
            GUARD.check_product(rules, product),
            GUARD.check_final(rules, product, checkout),
        ):
            for check in report.checks:
                assert check.title and check.title[0].isupper()
                assert "_" not in check.title


class TestWrongItem:
    def test_different_asin_blocks(self, rules, product) -> None:
        wrong = ProductSnapshot(**{**product.__dict__, "asin": "B0DIFFERENT"})
        report = GUARD.check_product(rules, wrong)
        assert not report.passed
        assert report.blocked_code is ErrorCode.ASIN_MISMATCH

    def test_missing_asin_blocks(self, rules, product) -> None:
        wrong = ProductSnapshot(**{**product.__dict__, "asin": ""})
        assert not GUARD.check_product(rules, wrong).passed

    def test_asin_comparison_ignores_case(self, rules, product) -> None:
        lowered = ProductSnapshot(**{**product.__dict__, "asin": ASIN.lower()})
        assert status_of(GUARD.check_product(rules, lowered), CHECK_ASIN) is CheckStatus.PASS

    def test_checkout_holding_a_different_item_blocks(
        self, rules, product, checkout
    ) -> None:
        wrong = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "lines": (CartLine(asin="B0OTHERITEM", title="Other", quantity=1),),
            }
        )
        report = GUARD.check_final(rules, product, wrong)
        assert not report.passed
        assert report.blocked_code is ErrorCode.ASIN_MISMATCH

    def test_checkout_with_unreadable_items_blocks(
        self, rules, product, checkout
    ) -> None:
        empty = CheckoutSnapshot(**{**checkout.__dict__, "lines": ()})
        assert not GUARD.check_final(rules, product, empty).passed


class TestWrongVariation:
    def test_changed_variation_blocks(self, rules, product) -> None:
        wrong = ProductSnapshot(
            **{**product.__dict__, "variation": VariationSnapshot({"Color": "Red"})}
        )
        report = GUARD.check_product(rules, wrong)
        assert not report.passed
        assert report.blocked_code is ErrorCode.VARIATION_CHANGED

    def test_missing_variation_blocks_when_one_was_expected(
        self, rules, product
    ) -> None:
        wrong = ProductSnapshot(
            **{**product.__dict__, "variation": VariationSnapshot()}
        )
        assert not GUARD.check_product(rules, wrong).passed

    def test_extra_dimension_blocks(self, rules, product) -> None:
        wrong = ProductSnapshot(
            **{
                **product.__dict__,
                "variation": VariationSnapshot({"Color": "Black", "Size": "XL"}),
            }
        )
        assert not GUARD.check_product(rules, wrong).passed

    def test_case_and_spacing_differences_do_not_block(self, rules, product) -> None:
        same = ProductSnapshot(
            **{**product.__dict__, "variation": VariationSnapshot({"color": " black "})}
        )
        assert status_of(GUARD.check_product(rules, same), CHECK_VARIATION) is CheckStatus.PASS

    def test_unvaried_product_is_not_applicable(self, product) -> None:
        plain_rules = PurchaseRules(expected_asin=ASIN, max_item_price=usd("120.00"))
        plain = ProductSnapshot(
            **{**product.__dict__, "variation": VariationSnapshot()}
        )
        report = GUARD.check_product(plain_rules, plain)
        assert status_of(report, CHECK_VARIATION) is CheckStatus.NOT_APPLICABLE
        assert report.passed


class TestWrongSeller:
    def test_third_party_blocks_under_amazon_only(self, rules, product) -> None:
        wrong = ProductSnapshot(**{**product.__dict__, "seller": "XYZ Marketplace LLC"})
        report = GUARD.check_product(rules, wrong)
        assert not report.passed
        assert status_of(report, CHECK_SELLER) is CheckStatus.FAIL

    def test_seller_named_like_amazon_blocks(self, rules, product) -> None:
        """A marketplace seller called 'Amazon.com Deals' must not pass."""
        for impostor in (
            "Amazon.com Deals",
            "AmazonBasics Store",
            "Amazon Warehouse",
            "Amazonn.com",
            "Best Amazon Deals",
        ):
            wrong = ProductSnapshot(**{**product.__dict__, "seller": impostor})
            report = GUARD.check_product(rules, wrong)
            assert not report.passed, f"{impostor} wrongly accepted"

    def test_missing_seller_blocks(self, rules, product) -> None:
        wrong = ProductSnapshot(**{**product.__dict__, "seller": None})
        assert not GUARD.check_product(rules, wrong).passed

    def test_seller_change_blocks_even_when_policy_allows_it(self, product) -> None:
        """'Any seller' still blocks when the seller changed since setup."""
        loose = PurchaseRules(
            expected_asin=ASIN,
            seller_policy=SellerPolicy.ANY,
            expected_seller="Amazon.com",
            condition_policy=ConditionPolicy.NEW_ONLY,
            max_item_price=usd("120.00"),
        )
        wrong = ProductSnapshot(**{**product.__dict__, "seller": "XYZ Marketplace LLC"})
        report = GUARD.check_product(loose, wrong)
        assert not report.passed
        assert status_of(report, CHECK_SELLER) is CheckStatus.PASS
        assert status_of(report, CHECK_SELLER_CHANGED) is CheckStatus.FAIL
        assert report.blocked_code is ErrorCode.SELLER_CHANGED

    def test_approved_list_accepts_exact_names_only(self, product) -> None:
        approved = PurchaseRules(
            expected_asin=ASIN,
            seller_policy=SellerPolicy.APPROVED_LIST,
            approved_sellers=("Tool Depot",),
            condition_policy=ConditionPolicy.NEW_ONLY,
        )
        ok = ProductSnapshot(**{**product.__dict__, "seller": "Tool Depot"})
        assert status_of(GUARD.check_product(approved, ok), CHECK_SELLER) is CheckStatus.PASS

        near = ProductSnapshot(**{**product.__dict__, "seller": "Tool Depot Outlet"})
        assert status_of(GUARD.check_product(approved, near), CHECK_SELLER) is CheckStatus.FAIL

    def test_manufacturer_policy_needs_a_brand(self, product) -> None:
        without_brand = PurchaseRules(
            expected_asin=ASIN,
            seller_policy=SellerPolicy.AMAZON_OR_MANUFACTURER,
            condition_policy=ConditionPolicy.NEW_ONLY,
        )
        third_party = ProductSnapshot(**{**product.__dict__, "seller": "Klein Tools"})
        assert (
            status_of(GUARD.check_product(without_brand, third_party), CHECK_SELLER)
            is CheckStatus.FAIL
        )

        with_brand = PurchaseRules(
            expected_asin=ASIN,
            seller_policy=SellerPolicy.AMAZON_OR_MANUFACTURER,
            brand="Klein Tools",
            condition_policy=ConditionPolicy.NEW_ONLY,
        )
        assert (
            status_of(GUARD.check_product(with_brand, third_party), CHECK_SELLER)
            is CheckStatus.PASS
        )


class TestWrongCondition:
    def test_used_blocks_under_new_only(self, rules, product) -> None:
        wrong = ProductSnapshot(**{**product.__dict__, "condition": ItemCondition.USED})
        report = GUARD.check_product(rules, wrong)
        assert not report.passed
        assert report.blocked_code is ErrorCode.CONDITION_NOT_ALLOWED

    def test_unknown_condition_blocks(self, rules, product) -> None:
        wrong = ProductSnapshot(
            **{**product.__dict__, "condition": ItemCondition.UNKNOWN}
        )
        assert status_of(GUARD.check_product(rules, wrong), CHECK_CONDITION) is CheckStatus.FAIL

    def test_used_like_new_is_not_new(self) -> None:
        """Amazon's 'Used - Like New' must never satisfy a New-only rule."""
        assert ItemCondition.parse("Used - Like New") is ItemCondition.USED
        assert not ConditionPolicy.NEW_ONLY.accepts(ItemCondition.parse("Used - Like New"))

    def test_renewed_blocks_under_allow_used(self, rules, product) -> None:
        """Allowing used does not silently allow refurbished."""
        allow_used = PurchaseRules(
            expected_asin=ASIN,
            condition_policy=ConditionPolicy.ALLOW_USED,
            seller_policy=SellerPolicy.ANY,
        )
        renewed = ProductSnapshot(
            **{**product.__dict__, "condition": ItemCondition.RENEWED}
        )
        assert (
            status_of(GUARD.check_product(allow_used, renewed), CHECK_CONDITION)
            is CheckStatus.FAIL
        )

    def test_new_is_always_acceptable(self) -> None:
        for policy in ConditionPolicy:
            assert policy.accepts(ItemCondition.NEW)


class TestPriceLimits:
    def test_price_above_item_limit_blocks(self, rules, product) -> None:
        wrong = ProductSnapshot(**{**product.__dict__, "price": usd("120.01")})
        report = GUARD.check_product(rules, wrong)
        assert not report.passed
        assert report.blocked_code is ErrorCode.PRICE_ABOVE_LIMIT

    def test_price_exactly_at_limit_passes(self, rules, product) -> None:
        edge = ProductSnapshot(**{**product.__dict__, "price": usd("120.00")})
        assert GUARD.check_product(rules, edge).passed

    def test_one_cent_over_blocks(self, rules, product) -> None:
        edge = ProductSnapshot(**{**product.__dict__, "price": usd("120.01")})
        assert not GUARD.check_product(rules, edge).passed

    def test_missing_price_blocks(self, rules, product) -> None:
        wrong = ProductSnapshot(**{**product.__dict__, "price": None})
        report = GUARD.check_product(rules, wrong)
        assert not report.passed
        assert status_of(report, CHECK_MAX_ITEM_PRICE) is CheckStatus.FAIL

    def test_currency_mismatch_blocks(self, rules, product) -> None:
        wrong = ProductSnapshot(
            **{**product.__dict__, "price": Money.from_decimal("100.00", "EUR")}
        )
        assert not GUARD.check_product(rules, wrong).passed

    def test_no_item_limit_is_reported_as_not_set(self, product) -> None:
        loose = PurchaseRules(expected_asin=ASIN, seller_policy=SellerPolicy.ANY)
        report = GUARD.check_product(loose, product)
        assert status_of(report, CHECK_MAX_ITEM_PRICE) is CheckStatus.SKIPPED
        assert report.passed


class TestOrderTotalLimit:
    def test_total_above_limit_blocks(self, rules, product, checkout) -> None:
        wrong = CheckoutSnapshot(**{**checkout.__dict__, "order_total": usd("135.01")})
        report = GUARD.check_final(rules, product, wrong)
        assert not report.passed
        assert report.blocked_code is ErrorCode.TOTAL_ABOVE_LIMIT

    def test_total_exactly_at_limit_passes(self, rules, product, checkout) -> None:
        edge = CheckoutSnapshot(**{**checkout.__dict__, "order_total": usd("135.00")})
        assert GUARD.check_final(rules, product, edge).passed

    def test_item_price_within_limit_but_total_over_blocks(
        self, rules, product, checkout
    ) -> None:
        """Shipping and tax must not be able to push the order past the cap."""
        expensive_shipping = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "shipping": usd("25.00"),
                "order_total": usd("142.94"),
            }
        )
        report = GUARD.check_final(rules, product, expensive_shipping)
        assert not report.passed
        assert status_of(report, CHECK_MAX_ITEM_PRICE) is CheckStatus.PASS
        assert status_of(report, CHECK_MAX_ORDER_TOTAL) is CheckStatus.FAIL

    def test_missing_total_blocks(self, rules, product, checkout) -> None:
        wrong = CheckoutSnapshot(**{**checkout.__dict__, "order_total": None})
        assert not GUARD.check_final(rules, product, wrong).passed

    def test_total_currency_mismatch_blocks(self, rules, product, checkout) -> None:
        wrong = CheckoutSnapshot(
            **{**checkout.__dict__, "order_total": Money.from_decimal("100.00", "GBP")}
        )
        assert not GUARD.check_final(rules, product, wrong).passed


class TestQuantity:
    def test_more_than_available_blocks(self, rules, product) -> None:
        many = PurchaseRules(**{**rules.__dict__, "quantity": 5})
        limited = ProductSnapshot(**{**product.__dict__, "max_quantity": 2})
        report = GUARD.check_product(many, limited)
        assert not report.passed
        assert report.blocked_code is ErrorCode.QUANTITY_UNAVAILABLE

    def test_checkout_quantity_mismatch_blocks(self, rules, product, checkout) -> None:
        wrong = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "lines": (
                    CartLine(asin=ASIN, title="x", quantity=3, unit_price=usd("109.97")),
                ),
            }
        )
        report = GUARD.check_final(rules, product, wrong)
        assert not report.passed
        assert status_of(report, CHECK_QUANTITY) is CheckStatus.FAIL

    def test_quantity_split_across_lines_is_summed(self, rules, product, checkout) -> None:
        split = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "lines": (
                    CartLine(asin=ASIN, title="x", quantity=1, unit_price=usd("109.97")),
                    CartLine(asin=ASIN, title="x", quantity=1, unit_price=usd("109.97")),
                ),
            }
        )
        report = GUARD.check_final(rules, product, split)
        assert status_of(report, CHECK_QUANTITY) is CheckStatus.FAIL


class TestCartIsolation:
    def test_unrelated_cart_item_blocks(self, rules, product, cart) -> None:
        polluted = CartState(
            lines=cart.lines
            + (CartLine(asin="B0UNRELATED", title="Dog food", quantity=2),),
            subtotal=usd("139.97"),
        )
        report = GUARD.check_cart(rules, product, polluted)
        assert not report.passed
        assert report.blocked_code is ErrorCode.UNEXPECTED_CART_ITEMS

    def test_item_with_no_asin_counts_as_unrelated(self, rules, product, cart) -> None:
        polluted = CartState(
            lines=cart.lines + (CartLine(asin=None, title="Mystery", quantity=1),)
        )
        assert not GUARD.check_cart(rules, product, polluted).passed

    def test_empty_cart_blocks(self, rules, product) -> None:
        report = GUARD.check_cart(rules, product, CartState(reported_empty=True))
        assert not report.passed
        assert status_of(report, CHECK_CART_CONTENTS) is CheckStatus.FAIL

    def test_unrelated_checkout_line_blocks(self, rules, product, checkout) -> None:
        polluted = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "lines": checkout.lines
                + (CartLine(asin="B0UNRELATED", title="Dog food", quantity=1),),
                "order_total": usd("130.00"),
            }
        )
        report = GUARD.check_final(rules, product, polluted)
        assert not report.passed
        assert status_of(report, CHECK_ADDONS) is CheckStatus.FAIL


class TestAddonsAndSubscriptions:
    def test_protection_plan_blocks(self, rules, product, checkout) -> None:
        with_addon = CheckoutSnapshot(
            **{**checkout.__dict__, "addons": ("3-Year Protection Plan",)}
        )
        report = GUARD.check_final(rules, product, with_addon)
        assert not report.passed
        assert report.blocked_code is ErrorCode.UNEXPECTED_ADDONS

    def test_addons_may_be_allowed_explicitly(self, rules, product, checkout) -> None:
        permissive = PurchaseRules(**{**rules.__dict__, "allow_addons": True})
        with_addon = CheckoutSnapshot(
            **{**checkout.__dict__, "addons": ("3-Year Protection Plan",)}
        )
        assert GUARD.check_final(permissive, product, with_addon).passed

    def test_preselected_subscription_blocks(self, rules, product) -> None:
        sns = ProductSnapshot(
            **{**product.__dict__, "subscription_preselected": True}
        )
        report = GUARD.check_product(rules, sns)
        assert not report.passed
        assert report.blocked_code is ErrorCode.SUBSCRIPTION_DETECTED

    def test_recurring_checkout_blocks(self, rules, product, checkout) -> None:
        recurring = CheckoutSnapshot(**{**checkout.__dict__, "is_subscription": True})
        report = GUARD.check_final(rules, product, recurring)
        assert not report.passed
        assert status_of(report, CHECK_SUBSCRIPTION) is CheckStatus.FAIL


class TestAddressAndPayment:
    def test_different_address_blocks(self, rules, product, checkout) -> None:
        wrong = CheckoutSnapshot(
            **{**checkout.__dict__, "address_label": "Someone Else, Miami, FL"}
        )
        report = GUARD.check_final(rules, product, wrong)
        assert not report.passed
        assert report.blocked_code is ErrorCode.ADDRESS_PROBLEM

    def test_missing_address_blocks(self, rules, product, checkout) -> None:
        wrong = CheckoutSnapshot(**{**checkout.__dict__, "address_label": None})
        assert status_of(GUARD.check_final(rules, product, wrong), CHECK_ADDRESS) is CheckStatus.FAIL

    def test_different_payment_blocks(self, rules, product, checkout) -> None:
        wrong = CheckoutSnapshot(
            **{**checkout.__dict__, "payment_label": "Mastercard ending 9999"}
        )
        report = GUARD.check_final(rules, product, wrong)
        assert not report.passed
        assert status_of(report, CHECK_PAYMENT) is CheckStatus.FAIL

    def test_missing_payment_blocks_even_without_an_expectation(
        self, product, checkout
    ) -> None:
        loose = PurchaseRules(
            expected_asin=ASIN,
            seller_policy=SellerPolicy.ANY,
            require_address_match=False,
            require_payment_match=False,
        )
        wrong = CheckoutSnapshot(**{**checkout.__dict__, "payment_label": ""})
        assert status_of(GUARD.check_final(loose, product, wrong), CHECK_PAYMENT) is CheckStatus.FAIL

    def test_matching_can_be_relaxed(self, rules, product, checkout) -> None:
        relaxed = PurchaseRules(
            **{**rules.__dict__, "require_address_match": False, "require_payment_match": False}
        )
        different = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "address_label": "Somewhere else",
                "payment_label": "Another card",
            }
        )
        assert GUARD.check_final(relaxed, product, different).passed


class TestPlaceOrderControl:
    def test_missing_order_button_blocks(self, rules, product, checkout) -> None:
        wrong = CheckoutSnapshot(
            **{**checkout.__dict__, "place_order_control_found": False}
        )
        report = GUARD.check_final(rules, product, wrong)
        assert not report.passed
        assert status_of(report, CHECK_PLACE_ORDER_CONTROL) is CheckStatus.FAIL


class TestPrime:
    def test_required_but_undetectable_blocks(self, rules, product) -> None:
        strict = PurchaseRules(**{**rules.__dict__, "require_prime": True})
        unknown = ProductSnapshot(**{**product.__dict__, "prime_eligible": None})
        report = GUARD.check_product(strict, unknown)
        assert not report.passed
        assert status_of(report, CHECK_PRIME) is CheckStatus.FAIL

    def test_not_required_is_not_applicable(self, rules, product) -> None:
        report = GUARD.check_product(rules, product)
        assert status_of(report, CHECK_PRIME) is CheckStatus.NOT_APPLICABLE

    def test_required_and_present_passes(self, rules, product) -> None:
        strict = PurchaseRules(**{**rules.__dict__, "require_prime": True})
        prime = ProductSnapshot(**{**product.__dict__, "prime_eligible": True})
        assert GUARD.check_product(strict, prime).passed


class TestAvailability:
    def test_out_of_stock_blocks(self, rules, product) -> None:
        wrong = ProductSnapshot(
            **{
                **product.__dict__,
                "availability": Availability.OUT_OF_STOCK,
                "availability_text": "Currently unavailable",
            }
        )
        report = GUARD.check_product(rules, wrong)
        assert not report.passed
        assert report.blocked_code is ErrorCode.PRODUCT_UNAVAILABLE

    def test_unknown_availability_blocks(self, rules, product) -> None:
        wrong = ProductSnapshot(
            **{**product.__dict__, "availability": Availability.UNKNOWN}
        )
        assert status_of(GUARD.check_product(rules, wrong), CHECK_AVAILABILITY) is CheckStatus.FAIL

    def test_preorder_blocks(self, rules, product) -> None:
        wrong = ProductSnapshot(
            **{**product.__dict__, "availability": Availability.PREORDER}
        )
        assert not GUARD.check_product(rules, wrong).passed


class TestTotalUnchanged:
    def test_same_total_passes(self) -> None:
        assert verify_total_unchanged(usd("117.94"), usd("117.94")).status is CheckStatus.PASS

    def test_increase_blocks(self) -> None:
        check = verify_total_unchanged(usd("117.94"), usd("118.94"))
        assert check.status is CheckStatus.FAIL
        assert check.error_code is ErrorCode.CHECKOUT_TOTAL_CHANGED

    def test_decrease_also_blocks(self) -> None:
        """Consent was given for a specific amount, not 'that or less'."""
        assert verify_total_unchanged(usd("117.94"), usd("110.00")).status is CheckStatus.FAIL

    def test_unreadable_total_blocks(self) -> None:
        assert verify_total_unchanged(usd("117.94"), None).status is CheckStatus.FAIL
        assert verify_total_unchanged(None, usd("117.94")).status is CheckStatus.FAIL


class TestReportInvariants:
    def test_a_report_with_a_blocking_failure_cannot_pass(self, rules, product) -> None:
        wrong = ProductSnapshot(**{**product.__dict__, "price": usd("999.00")})
        report = GUARD.check_product(rules, wrong)
        assert report.blocking_failures
        assert not report.passed

    def test_summary_counts_only_real_checks(self, rules, product, checkout) -> None:
        report = GUARD.check_final(rules, product, checkout)
        assert report.summary == "All checks passed"

    def test_failure_summary_is_informative(self, rules, product) -> None:
        wrong = ProductSnapshot(**{**product.__dict__, "price": usd("999.00")})
        summary = GUARD.check_product(rules, wrong).summary
        assert "of" in summary and "checks passed" in summary

    def test_checks_are_in_display_order(self, rules, product, checkout) -> None:
        from app.purchasing.purchase_guard import CHECK_ORDER

        report = GUARD.check_final(rules, product, checkout)
        positions = [CHECK_ORDER.index(c.check_id) for c in report.checks]
        assert positions == sorted(positions)

    def test_all_failures_carry_an_error_code(self, rules, product, checkout) -> None:
        """Every blocking failure must map to a user-facing message."""
        from app.core.errors import describe

        broken = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "order_total": None,
                "address_label": None,
                "payment_label": None,
                "place_order_control_found": False,
                "addons": ("Protection Plan",),
                "is_subscription": True,
            }
        )
        report = GUARD.check_final(rules, product, broken)
        assert report.blocking_failures
        for check in report.blocking_failures:
            assert check.error_code is not None, check.check_id
            assert describe(check.error_code).title
