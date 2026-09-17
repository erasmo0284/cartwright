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


class TestIdentityWithoutAnItemCode:
    """Amazon's current checkout usually prints no ASIN on the line.

    A live third-party order on 2026-09-16 carried none at all: no
    ``data-asin``, no product link, nothing but a line-item id and the
    seller. The Amazon-sold order that passed earlier only had one because a
    Subscribe & Save upsell inside the row happened to include it.

    So the title is used -- narrowly. Everything in this class is about the
    boundary of that fallback, because it is the one place the guard accepts
    something weaker than an exact code.
    """

    def _unlabelled(self, checkout, product, **overrides):
        """The checkout as Amazon serves it: one line, no code, real title."""
        line = CartLine(
            asin=None,
            title=product.title,
            quantity=None,
            unit_price=usd("109.97"),
        )
        fields = {**checkout.__dict__, "lines": (line,), **overrides}
        return CheckoutSnapshot(**fields)

    def test_one_unlabelled_line_matching_the_title_is_accepted(
        self, rules, product, checkout
    ) -> None:
        report = GUARD.check_final(rules, product, self._unlabelled(checkout, product))
        assert status_of(report, CHECK_ASIN) is CheckStatus.PASS
        assert report.passed, report.summary

    def test_the_acceptance_says_how_the_item_was_identified(
        self, rules, product, checkout
    ) -> None:
        """The user is told the code was not shown, not left to assume it was."""
        report = GUARD.check_final(rules, product, self._unlabelled(checkout, product))
        check = next(c for c in report.checks if c.check_id == CHECK_ASIN)
        assert "matched by name" in (check.actual or "")

    def test_a_brand_prefixed_title_is_accepted(
        self, rules, product, checkout
    ) -> None:
        """Amazon's checkout puts the brand in front of the title for some items.

        Live, a page reading "GQZMBM 16 Pcs/lot ..." produced a checkout line
        reading "JINSUO GQZMBM 16 Pcs/lot ...". Still matched as an equality
        against a second exact form, not as a prefix rule.
        """
        branded_product = ProductSnapshot(
            **{**product.__dict__, "brand": "Klein Tools"}
        )
        branded = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "lines": (
                    CartLine(
                        asin=None,
                        title=f"Klein Tools {product.title}",
                        quantity=None,
                        unit_price=usd("109.97"),
                    ),
                ),
            }
        )
        report = GUARD.check_final(rules, branded_product, branded)
        assert status_of(report, CHECK_ASIN) is CheckStatus.PASS
        assert report.passed, report.summary

    def test_another_brands_prefix_is_refused(
        self, rules, product, checkout
    ) -> None:
        """Only *this* product's brand, not any word in front of the title."""
        branded_product = ProductSnapshot(
            **{**product.__dict__, "brand": "Klein Tools"}
        )
        wrong = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "lines": (
                    CartLine(
                        asin=None,
                        title=f"Acme {product.title}",
                        quantity=None,
                        unit_price=usd("109.97"),
                    ),
                ),
            }
        )
        assert status_of(
            GUARD.check_final(rules, branded_product, wrong), CHECK_ASIN
        ) is CheckStatus.FAIL

    def test_a_different_title_is_refused(self, rules, product, checkout) -> None:
        """This is the whole point: a substituted item has a different name."""
        wrong = self._unlabelled(checkout, product)
        wrong = CheckoutSnapshot(
            **{
                **wrong.__dict__,
                "lines": (
                    CartLine(
                        asin=None,
                        title="Something else entirely",
                        quantity=None,
                        unit_price=usd("109.97"),
                    ),
                ),
            }
        )
        report = GUARD.check_final(rules, product, wrong)
        assert status_of(report, CHECK_ASIN) is CheckStatus.FAIL
        assert not report.passed

    def test_a_title_that_only_starts_the_same_is_refused(
        self, rules, product, checkout
    ) -> None:
        """Equality, not a prefix: accessories are named after their product."""
        near = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "lines": (
                    CartLine(
                        asin=None,
                        title=f"Case for {product.title}",
                        quantity=None,
                        unit_price=usd("109.97"),
                    ),
                ),
            }
        )
        assert status_of(
            GUARD.check_final(rules, product, near), CHECK_ASIN
        ) is CheckStatus.FAIL

    def test_a_missing_title_is_refused(self, rules, product, checkout) -> None:
        blank = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "lines": (
                    CartLine(asin=None, title=None, quantity=None, unit_price=usd("109.97")),
                ),
            }
        )
        assert status_of(
            GUARD.check_final(rules, product, blank), CHECK_ASIN
        ) is CheckStatus.FAIL

    def test_a_second_line_refuses_the_whole_thing(
        self, rules, product, checkout
    ) -> None:
        """With two lines, nothing can be identified by elimination."""
        two = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "lines": (
                    CartLine(asin=None, title=product.title, quantity=None,
                             unit_price=usd("109.97")),
                    CartLine(asin=None, title="Dog food", quantity=None,
                             unit_price=usd("30.00")),
                ),
            }
        )
        report = GUARD.check_final(rules, product, two)
        assert status_of(report, CHECK_ASIN) is CheckStatus.FAIL
        assert not report.passed

    def test_a_line_bearing_someone_elses_code_is_refused(
        self, rules, product, checkout
    ) -> None:
        """A wrong code is a different item, not an unlabelled one.

        Without this, an item whose page title happened to match would be
        accepted *despite* Amazon stating a different ASIN for it.
        """
        labelled = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "lines": (
                    CartLine(
                        asin="B0OTHERITEM",
                        title=product.title,
                        quantity=1,
                        unit_price=usd("109.97"),
                    ),
                ),
            }
        )
        assert status_of(
            GUARD.check_final(rules, product, labelled), CHECK_ASIN
        ) is CheckStatus.FAIL

    def test_the_price_and_quantity_checks_see_the_identified_line(
        self, rules, product, checkout
    ) -> None:
        """Identification has to reach the other checks, or they contradict it.

        Before this, the ASIN check passed by title while the price check
        reported "not shown at checkout" and the item counted as foreign --
        three checks disagreeing about the same line.
        """
        report = GUARD.check_final(rules, product, self._unlabelled(checkout, product))
        assert status_of(report, CHECK_MAX_ITEM_PRICE) is CheckStatus.PASS
        assert status_of(report, CHECK_QUANTITY) is CheckStatus.PASS
        assert status_of(report, CHECK_CART_CONTENTS) is CheckStatus.PASS

    def test_an_over_limit_price_still_blocks_on_an_unlabelled_line(
        self, rules, product, checkout
    ) -> None:
        """Identifying the line must not soften what is checked about it."""
        dear = self._unlabelled(
            checkout,
            product,
            lines=(
                CartLine(
                    asin=None, title=product.title, quantity=None,
                    unit_price=usd("500.00"),
                ),
            ),
        )
        report = GUARD.check_final(rules, product, dear)
        assert status_of(report, CHECK_MAX_ITEM_PRICE) is CheckStatus.FAIL
        assert not report.passed


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


class TestOrderTotalLimitIsMandatory:
    """The order total is the only figure that sees the real charge."""

    def test_an_unset_order_total_limit_blocks(self, rules, product, checkout) -> None:
        unbounded = PurchaseRules(**{**rules.__dict__, "max_order_total": None})
        report = GUARD.check_final(unbounded, product, checkout)
        assert not report.passed
        assert status_of(report, CHECK_MAX_ORDER_TOTAL) is CheckStatus.FAIL

    def test_an_item_limit_alone_does_not_bound_the_order(
        self, rules, product, checkout
    ) -> None:
        """Shipping, fees and quantity all land on the order total."""
        unbounded = PurchaseRules(
            **{**rules.__dict__, "max_order_total": None, "max_item_price": usd("120.00")}
        )
        expensive = CheckoutSnapshot(
            **{**checkout.__dict__, "shipping": usd("1800.00"), "order_total": usd("1999.97")}
        )
        report = GUARD.check_final(unbounded, product, expensive)
        assert not report.passed
        assert status_of(report, CHECK_MAX_ITEM_PRICE) is CheckStatus.PASS
        assert status_of(report, CHECK_MAX_ORDER_TOTAL) is CheckStatus.FAIL


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

    def test_an_unshown_quantity_is_confirmed_from_the_item_subtotal(
        self, rules, product, checkout
    ) -> None:
        """Amazon's current checkout prints no quantity when there is one.

        Confirmed on a live Buy Now order on 2026-09-16: no "Qty" anywhere on
        the page. Refusing outright would block every purchase; fabricating a
        1 would validate a number nobody observed. The arithmetic is the third
        option -- the item subtotal has to be exactly the unit price times the
        quantity the user asked for, which only the right number satisfies.
        """
        unshown = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "lines": (
                    CartLine(
                        asin=ASIN, title="x", quantity=None, unit_price=usd("109.97")
                    ),
                ),
                "item_subtotal": usd("109.97"),
            }
        )
        report = GUARD.check_final(rules, product, unshown)
        assert status_of(report, CHECK_QUANTITY) is CheckStatus.PASS
        assert report.passed

    def test_an_unshown_quantity_whose_subtotal_disagrees_blocks(
        self, rules, product, checkout
    ) -> None:
        """Three of them, with the quantity not printed: the money gives it away."""
        three = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "lines": (
                    CartLine(
                        asin=ASIN, title="x", quantity=None, unit_price=usd("109.97")
                    ),
                ),
                "item_subtotal": usd("329.91"),
            }
        )
        report = GUARD.check_final(rules, product, three)
        assert status_of(report, CHECK_QUANTITY) is CheckStatus.FAIL
        assert not report.passed

    def test_an_unshown_quantity_with_no_subtotal_blocks(
        self, rules, product, checkout
    ) -> None:
        """Nothing to derive from means nothing is derived."""
        blank = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "lines": (
                    CartLine(
                        asin=ASIN, title="x", quantity=None, unit_price=usd("109.97")
                    ),
                ),
                "item_subtotal": None,
            }
        )
        assert status_of(
            GUARD.check_final(rules, product, blank), CHECK_QUANTITY
        ) is CheckStatus.FAIL

    def test_an_unshown_quantity_with_no_unit_price_blocks(
        self, rules, product, checkout
    ) -> None:
        unpriced = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "lines": (
                    CartLine(asin=ASIN, title="x", quantity=None, unit_price=None),
                ),
                "item_subtotal": usd("109.97"),
            }
        )
        assert status_of(
            GUARD.check_final(rules, product, unpriced), CHECK_QUANTITY
        ) is CheckStatus.FAIL

    def test_a_discounted_subtotal_is_not_treated_as_a_quantity(
        self, rules, product, checkout
    ) -> None:
        """A subtotal that is not a whole multiple must never pass.

        A promotion, a coupon or a second line all break the arithmetic, and
        breaking it is what makes this safe.
        """
        discounted = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "lines": (
                    CartLine(
                        asin=ASIN, title="x", quantity=None, unit_price=usd("109.97")
                    ),
                ),
                "item_subtotal": usd("99.00"),
            }
        )
        assert status_of(
            GUARD.check_final(rules, product, discounted), CHECK_QUANTITY
        ) is CheckStatus.FAIL

    def test_a_second_line_stops_the_subtotal_being_used(
        self, rules, product, checkout
    ) -> None:
        """With another item in the order the subtotal says nothing about ours."""
        two_lines = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "lines": (
                    CartLine(
                        asin=ASIN, title="x", quantity=None, unit_price=usd("109.97")
                    ),
                    CartLine(
                        asin="B0OTHER0001", title="y", quantity=1, unit_price=usd("5.00")
                    ),
                ),
                "item_subtotal": usd("109.97"),
            }
        )
        assert status_of(
            GUARD.check_final(rules, product, two_lines), CHECK_QUANTITY
        ) is CheckStatus.FAIL

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
        """A foreign product is caught by the cart-contents check."""
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
        assert status_of(report, CHECK_CART_CONTENTS) is CheckStatus.FAIL
        assert report.blocked_code is ErrorCode.UNEXPECTED_CART_ITEMS
        # It is not an "add-on": nothing was attached, something else is there.
        assert status_of(report, CHECK_ADDONS) is CheckStatus.PASS

    def test_allowing_addons_does_not_allow_a_foreign_product(
        self, rules, product, checkout
    ) -> None:
        """"Allow a protection plan" must not mean "buy anything else too"."""
        permissive = PurchaseRules(**{**rules.__dict__, "allow_addons": True})
        polluted = CheckoutSnapshot(
            **{
                **checkout.__dict__,
                "lines": checkout.lines
                + (
                    CartLine(
                        asin="B0TELEVISION",
                        title="65-inch television",
                        quantity=1,
                        unit_price=usd("899.00"),
                    ),
                ),
                "order_total": usd("130.00"),
            }
        )
        report = GUARD.check_final(permissive, product, polluted)
        assert not report.passed
        assert status_of(report, CHECK_CART_CONTENTS) is CheckStatus.FAIL


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
