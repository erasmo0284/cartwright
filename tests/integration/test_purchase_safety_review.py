"""Adversarial purchase-safety review, against real Chromium.

These exercise the same readers the application uses, on real DOM, through
the intercepted fixture site -- no network, so no order can be placed.

Each test is an attack on the checkout reader: it changes only Amazon's
*wording* or *element type*, never the amounts, and checks that the guard is
never handed a value that is more favourable than what the page says.

These began as defect proofs pinning readings that were wrong. The readers
have since been fixed, so each one now pins the corrected reading; the
docstrings say what the old reading was, because that is what explains why
the assertion is worth making.
"""

from __future__ import annotations

import pytest

from app.automation.checkout_manager import MANAGER as CHECKOUT
from app.core.money import Money
from app.purchasing.models import (
    Availability,
    ConditionPolicy,
    ItemCondition,
    ProductSnapshot,
    PurchaseRules,
    SellerPolicy,
)
from app.purchasing.purchase_guard import (
    CHECK_CHECKOUT_SELLER,
    CHECK_MAX_ORDER_TOTAL,
    CHECK_QUANTITY,
    GUARD,
)
from app.purchasing.validation import CheckStatus
from tests.fixtures import amazon_pages as pages

pytestmark = pytest.mark.integration

ASIN = pages.DEFAULT_ASIN
CHECKOUT_URL = "https://www.amazon.com/gp/buy/spc/handlers/display.html"


def usd(amount: str) -> Money:
    return Money.from_decimal(amount, "USD")


def _product() -> ProductSnapshot:
    return ProductSnapshot(
        asin=ASIN,
        title=pages.DEFAULT_TITLE,
        price=usd("109.97"),
        availability=Availability.IN_STOCK,
        availability_text="In Stock",
        seller="Amazon.com",
        condition=ItemCondition.NEW,
        prime_eligible=True,
    )


def _rules(**overrides) -> PurchaseRules:
    base = dict(
        expected_asin=ASIN,
        quantity=1,
        max_item_price=usd("120.00"),
        max_order_total=None,
        seller_policy=SellerPolicy.ANY,
        condition_policy=ConditionPolicy.NEW_ONLY,
        require_address_match=False,
        require_payment_match=False,
    )
    base.update(overrides)
    return PurchaseRules(**base)


def status_of(report, check_id: str) -> CheckStatus:
    check = report.find(check_id)
    assert check is not None
    return check.status


class TestCheckoutQuantityWording:
    def test_the_fixture_wording_is_read_correctly(self, load) -> None:
        """The control: "Qty: 3", Amazon's classic wording, reads as 3.

        Every other test in this class asserts that a quantity is *not*
        fabricated as 1. This one is what stops that being satisfied by a
        reader that answers "unknown" to everything and blocks every order.
        """
        reader = load(CHECKOUT_URL, pages.checkout_page(quantity=3))
        snapshot = CHECKOUT.read_checkout(reader)
        assert snapshot.lines[0].quantity == 3

    @pytest.mark.parametrize(
        "replacement",
        [
            pytest.param('<span class="quantity">Quantity: 3</span>', id="worded"),
            pytest.param(
                '<select class="quantity" name="quantity">'
                '<option value="1">1</option>'
                '<option value="3" selected>3</option></select>',
                id="select",
            ),
            pytest.param('<span class="quantity">3 x</span>', id="multiplier"),
        ],
    )
    def test_a_quantity_the_reader_misses_is_never_fabricated_as_one(
        self, load, replacement: str
    ) -> None:
        """ATTACK 2/3 DEFEATED: three units are never read, or bought, as one.

        ``CheckoutManager._line_quantity`` once had a single reader --
        ``re.compile(r"qty\\s*:?\\s*(\\d+)")`` over the row's text -- and fell
        back to ``quantity = 1`` whenever it did not match. Anything else
        Amazon might render, including the ``<select>`` its newer checkout
        uses, silently became 1, and the guard then *validated* that
        fabricated 1 against ``rules.quantity == 1`` and reported PASS.

        It now tries the form control first and returns ``None`` when nothing
        answers, so each of these three markups reads either the real 3 or
        "unknown" -- and ``_check_quantity_in_checkout`` blocks on both. The
        assertion is deliberately written as "never 1" rather than pinning a
        particular reading per markup, because either outcome is safe and
        which one applies depends on how many wordings the reader covers.
        """
        html = pages.checkout_page(
            quantity=3, item_subtotal="329.91", tax="23.91", order_total="353.82"
        ).replace('<span class="quantity">Qty: 3</span>', replacement)
        assert replacement in html, "the fixture markup changed"

        reader = load(CHECKOUT_URL, html)
        snapshot = CHECKOUT.read_checkout(reader)

        quantity = snapshot.lines[0].quantity
        assert quantity in (None, 3), quantity  # never a fabricated 1
        assert snapshot.order_total == usd("353.82")  # the real charge

        report = GUARD.check_final(_rules(), _product(), snapshot)
        assert status_of(report, CHECK_QUANTITY) is CheckStatus.FAIL
        assert not report.passed  # one item's worth of checking is not enough


class TestOrderTotalReading:
    def test_a_total_row_with_a_second_colon_is_read_at_its_largest(
        self, load
    ) -> None:
        """ATTACK 2 DEFEATED: the total is read as the largest number in its row.

        ``_read_summary`` used to keep only ``text.rpartition(":")[2]`` -- the
        text after the *last* colon -- and parse that, while the label match
        used ``split(":")[0]``. So a row carrying a second colon was still
        classified as the order total but took its value from the wrong
        fragment, and ``parse_money_ceiling``'s promise that a mis-read can
        only ever block was bypassed: the truncation happened before the
        parser saw the text.

        The whole row now goes to the parser, which takes the maximum, so the
        guard sees the real charge and the $150 ceiling refuses it.
        """
        # Amazon's own fine print, rendered inside the grand-total row. The
        # label cell is untouched, so the row is still classified as the
        # order total.
        html = pages.checkout_page(order_total="1299.00").replace(
            "$1299.00</span>",
            "$1,299.00</span>"
            '<span class="a-size-mini a-color-secondary">'
            "of which estimated tax: $104.00</span>",
        )
        reader = load(CHECKOUT_URL, html)
        snapshot = CHECKOUT.read_checkout(reader)

        assert snapshot.order_total == usd("1299.00")

        report = GUARD.check_final(
            _rules(max_order_total=usd("150.00")), _product(), snapshot
        )
        assert status_of(report, CHECK_MAX_ORDER_TOTAL) is CheckStatus.FAIL
        assert not report.passed  # a $150 ceiling refusing a $1,299 order

    def test_a_total_row_without_a_colon_blocks(self, load) -> None:
        """An unrecognised total row is dropped, and a dropped row blocks.

        The label is still matched on ``split(":")[0]``, so with no colon at
        all it no longer equals any of ``ORDER_TOTAL_LABELS`` and the row is
        ignored. That leaves ``order_total`` as ``None``, which
        ``_check_order_total_known`` turns into a block -- so the one part of
        the row reader that is still wording-sensitive fails in the blocking
        direction.
        """
        html = pages.checkout_page().replace("Order total:", "Order total")
        reader = load(CHECKOUT_URL, html)
        snapshot = CHECKOUT.read_checkout(reader)

        assert snapshot.order_total is None
        assert not GUARD.check_final(_rules(), _product(), snapshot).passed


class TestSellerAtCheckout:
    def test_the_checkout_reader_records_the_seller_on_the_line(self, load) -> None:
        """ATTACK 4 DEFEATED: the bought offer is attributed to a seller.

        ``CartLine.seller`` existed and the cart reader filled it in, but
        ``CheckoutManager._read_lines`` never set it and no guard check read
        it -- so the only seller the guard saw at PRE_SUBMIT was the one read
        on the *product page*, minutes earlier, and a buybox that changed
        hands in between was invisible.

        ``_line_seller`` now reads the "Sold by" text off the checkout line,
        stripping Amazon's label prefixes, and ``_check_checkout_seller``
        judges it against the same seller policy.
        """
        html = pages.checkout_page().replace(
            f'<span class="a-size-base sc-product-title">{pages.DEFAULT_TITLE}</span>',
            f'<span class="a-size-base sc-product-title">{pages.DEFAULT_TITLE}</span>'
            '<span class="sc-product-sold-by">Sold by: Bargain Bin Electronics</span>',
        )
        reader = load(CHECKOUT_URL, html)
        snapshot = CHECKOUT.read_checkout(reader)

        assert snapshot.lines[0].seller == "Bargain Bin Electronics"
        report = GUARD.check_final(
            _rules(seller_policy=SellerPolicy.AMAZON_ONLY), _product(), snapshot
        )
        assert status_of(report, CHECK_CHECKOUT_SELLER) is CheckStatus.FAIL
        assert not report.passed
