"""Adversarial purchase-safety review.

Each test is an *attack*: it tries to get a wrong, duplicate or over-budget
order authorised through the real code paths.

Read the docstrings, not just the names. Every test asserts the behaviour
that exists **today**, so the whole file is green:

* a test whose docstring says "ATTACK N DEFEATED" pins a protection that
  works, so a future change cannot quietly remove it. Each one still drives
  the real code path, so reverting the protection turns the test red.
* a test whose docstring says "ATTACK N SUCCEEDS" is a defect proof: it pins
  behaviour that is still wrong, so fixing it turns the test red and forces
  whoever fixes it to rewrite the assertion as a regression test.

Most of this file began as defect proofs. Those defects have been fixed, so
the assertions now read the other way round -- they are regression pins on
the fix, and the docstring of each says which protection it holds in place.

Nothing here touches a browser or a network; the checkout and cart readers
are driven through small fakes of the Playwright API they use. The
markup-dependent findings are reproduced against real Chromium in
``tests/integration/test_purchase_safety_review.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.automation.checkout_manager import MANAGER as CHECKOUT
from app.core.errors import AppError, ErrorCode
from app.core.money import Money, parse_money_ceiling
from app.database.database import Database
from app.database.repositories import (
    ProductRepository,
    PurchaseRepository,
    RulesRepository,
)
from app.purchasing.models import (
    Availability,
    CartLine,
    CheckoutSnapshot,
    ConditionPolicy,
    ItemCondition,
    ProductSnapshot,
    PurchaseMode,
    PurchaseRules,
    SellerPolicy,
    VariationSnapshot,
    is_amazon_retail,
    normalise_label,
)
from app.purchasing.purchase_guard import (
    CHECK_ADDONS,
    CHECK_CART_CONTENTS,
    CHECK_CART_QUANTITY,
    CHECK_CHECKOUT_SELLER,
    CHECK_ITEM_PRICE,
    CHECK_MAX_ORDER_TOTAL,
    CHECK_QUANTITY,
    CHECK_SELLER,
    CHECK_VARIATION,
    GUARD,
)
from app.purchasing.states import PurchaseState
from app.purchasing.validation import CheckStatus

ASIN = "B07XYZ1234"


def usd(amount: str) -> Money:
    return Money.from_decimal(amount, "USD")


def status_of(report: Any, check_id: str) -> CheckStatus:
    check = report.find(check_id)
    assert check is not None, f"no check {check_id} in {report.phase}"
    return check.status


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def products(database: Database) -> ProductRepository:
    return ProductRepository(database)


@pytest.fixture
def rules_repo(database: Database) -> RulesRepository:
    return RulesRepository(database)


@pytest.fixture
def purchases(database: Database) -> PurchaseRepository:
    return PurchaseRepository(database)


@pytest.fixture
def snapshot() -> ProductSnapshot:
    return ProductSnapshot(
        asin=ASIN,
        title="Klein Tools CL800 Clamp Meter",
        brand="Klein Tools",
        url=f"https://www.amazon.com/dp/{ASIN}",
        price=usd("109.97"),
        availability=Availability.IN_STOCK,
        availability_text="In Stock",
        seller="Amazon.com",
        ships_from="Amazon.com",
        condition=ItemCondition.NEW,
        variation=VariationSnapshot({"Color": "Black"}),
        max_quantity=30,
        prime_eligible=True,
        buy_now_available=True,
        add_to_cart_available=True,
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
        expected_address_label="John D., Raleigh, NC 27601",
        expected_payment_label="Visa ending in 1234",
        brand="Klein Tools",
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
        shipping=Money.zero(),
        tax=usd("7.97"),
        order_total=usd("117.94"),
        address_label="John D., Raleigh, NC 27601",
        payment_label="Visa ending in 1234",
        place_order_control_found=True,
    )


def _job(purchases, products, rules_repo, snapshot, rules, *, state=None):
    """A purchase job for ``snapshot``, advanced to ``state``."""
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
    ):
        job = purchases.transition(job.id, step)
        if step is state:
            return product, job
    if state is not None and state is not PurchaseState.FINAL_VALIDATION:
        job = purchases.transition(job.id, state)
    return product, job


# ---------------------------------------------------------------------------
# Fake Playwright objects, so the readers can be driven without a browser.
# ---------------------------------------------------------------------------


class FakeLocator:
    """The slice of Playwright's Locator API the readers actually call."""

    def __init__(
        self,
        *,
        text: str | None = None,
        count: int = 1,
        attributes: dict[str, str] | None = None,
        children: dict[str, "FakeLocator"] | None = None,
        items: list["FakeLocator"] | None = None,
        raises: bool = False,
    ) -> None:
        self._text = text
        self._count = count
        self._attributes = attributes or {}
        self._children = children or {}
        self._items = items or []
        self._raises = raises

    # -- structure
    @property
    def first(self) -> "FakeLocator":
        return self._items[0] if self._items else self

    def count(self) -> int:
        return len(self._items) if self._items else self._count

    def nth(self, index: int) -> "FakeLocator":
        return self._items[index]

    def locator(self, selector: str) -> "FakeLocator":
        for key, child in self._children.items():
            if key in selector:
                return child
        return FakeLocator(count=0)

    # -- values
    def text_content(self, timeout: int | None = None) -> str | None:
        return self._text

    def inner_text(self, timeout: int | None = None) -> str | None:
        if self._raises:
            raise TimeoutError("inner_text timed out")
        return self._text

    def get_attribute(self, name: str, timeout: int | None = None) -> str | None:
        return self._attributes.get(name)

    def input_value(self, timeout: int | None = None) -> str | None:
        return self._attributes.get("value")

    def is_visible(self) -> bool:
        return True

    def is_checked(self, timeout: int | None = None) -> bool:
        return bool(self._attributes.get("checked"))


class FakePage:
    """Answers ``locator()`` from a selector-substring map."""

    def __init__(
        self,
        mapping: dict[str, FakeLocator],
        url: str = "https://x/",
        html: str = "<html></html>",
    ) -> None:
        self._mapping = mapping
        self.url = url
        self._html = html

    def locator(self, selector: str) -> FakeLocator:
        for key, value in self._mapping.items():
            if key in selector:
                return value
        return FakeLocator(count=0)

    def frame_locator(self, selector: str) -> FakeLocator:  # pragma: no cover
        return FakeLocator(count=0)

    def title(self) -> str:
        return "fake"

    def content(self) -> str:
        return self._html

    def wait_for_timeout(self, ms: float) -> None:
        return None


# ---------------------------------------------------------------------------
# Attack 1 -- a duplicate order
# ---------------------------------------------------------------------------


class TestAttack1Duplicates:
    def test_a_recorded_submission_is_recovered_as_uncertain(
        self, purchases, products, rules_repo, snapshot, rules
    ) -> None:
        """ATTACK 1 DEFEATED: crash between record_submission() and the click.

        ``mark_submitted`` is permitted from ``FINAL_VALIDATION`` and
        ``AWAITING_CONFIRMATION`` as well as ``SUBMITTING``, so the job may
        still be in a pre-submit state at the moment the submission is
        recorded. That window is exactly what the "record before the click"
        ordering exists to cover, and it is what startup recovery has to get
        right.

        ``recover_interrupted`` checks ``has_submitted`` before it decides,
        and sends such a job to ``UNKNOWN``. Every consequence that makes a
        duplicate order possible depends on ``FAILED`` being chosen instead:

        * ``UNKNOWN`` is not terminal and is a live state, so the product's
          single purchase slot stays held and a second ``create`` is refused;
        * the id comes back in the uncertain list, so
          ``recover_after_restart`` raises it with the user;
        * ``order_may_exist`` is true, so nothing in the UI claims that
          nothing was ordered.
        """
        product, job = _job(
            purchases,
            products,
            rules_repo,
            snapshot,
            rules,
            state=PurchaseState.AWAITING_CONFIRMATION,
        )
        attempt = purchases.begin_attempt(job.id)
        purchases.mark_submitted(attempt)
        assert purchases.has_submitted(job.id)

        # The process dies here, between the record and the click.
        uncertain = purchases.recover_interrupted()

        recovered = purchases.get(job.id)
        assert recovered.state is PurchaseState.UNKNOWN
        assert not recovered.state.is_terminal
        # The user is told an order may exist.
        assert uncertain == [job.id]
        assert recovered.state.order_may_exist
        # And the product is still held, so no second order can be started.
        assert purchases.live_job_for_product(product.id) is not None
        with pytest.raises(AppError) as excinfo:
            purchases.create(
                product_id=product.id,
                rules_id=job.rules_id,
                mode=PurchaseMode.AUTOMATIC,
                test_mode=False,
            )
        assert excinfo.value.code is ErrorCode.DUPLICATE_BLOCKED

    def test_recording_a_submission_pre_transition_recovers_as_uncertain(
        self, purchases, products, rules_repo, snapshot, rules
    ) -> None:
        """The same protection from the other pre-submit state.

        ``SUBMIT_RECORD_STATES`` deliberately permits recording a submission
        from ``FINAL_VALIDATION`` and ``AWAITING_CONFIRMATION`` as well as
        ``SUBMITTING``, so "submitted attempt while the job is still in a
        pre-submit state" is a position the repository's own contract allows.
        Recovery must reach ``UNKNOWN`` from either of them, not only from
        the two states whose *name* says a submission was in progress.
        """
        from app.purchasing.states import SUBMIT_RECORD_STATES

        assert PurchaseState.FINAL_VALIDATION in SUBMIT_RECORD_STATES
        assert PurchaseState.AWAITING_CONFIRMATION in SUBMIT_RECORD_STATES

        _product, job = _job(
            purchases,
            products,
            rules_repo,
            snapshot,
            rules,
            state=PurchaseState.FINAL_VALIDATION,
        )
        attempt = purchases.begin_attempt(job.id)
        purchases.mark_submitted(attempt)  # accepted, no exception
        assert purchases.has_submitted(job.id)

        assert purchases.recover_interrupted() == [job.id]
        # 'submitting' and 'confirming' become UNKNOWN, and so does this one.
        assert purchases.get(job.id).state is PurchaseState.UNKNOWN


# ---------------------------------------------------------------------------
# Attack 2 -- buying above a limit
# ---------------------------------------------------------------------------


class TestAttack2OverBudget:
    def test_an_item_price_limit_alone_does_not_leave_the_total_unbounded(
        self, rules, checkout
    ) -> None:
        """ATTACK 2 DEFEATED: ``max_order_total=None`` is a FAILING check.

        The order total is the only figure that sees the real charge --
        shipping, tax, fees and quantity all land there -- so an unset limit
        is absent *required* data, not "no limit". If it were treated as a
        SKIPPED check it would be ``required=False`` and therefore pass, and
        a $1,999.97 order would be authorised against a $120 item limit.

        Nothing in ``PurchaseRules``, the repository schema or the rules
        editor forces an order-total limit to exist, so the guard is the only
        thing standing between an incomplete rule and unbounded spending.
        """
        loose = PurchaseRules(
            **{
                **rules.__dict__,
                "max_order_total": None,
            }
        )
        runaway = CheckoutSnapshot(
            lines=checkout.lines,
            item_subtotal=usd("109.97"),
            shipping=usd("1800.00"),  # a mis-set shipping option, or a fee
            tax=usd("90.00"),
            order_total=usd("1999.97"),
            address_label=checkout.address_label,
            payment_label=checkout.payment_label,
            place_order_control_found=True,
        )
        report = GUARD.check_final(loose, _product_for(rules), runaway)
        assert status_of(report, CHECK_MAX_ORDER_TOTAL) is CheckStatus.FAIL
        assert not report.passed  # not authorised to spend $1,999.97
        assert report.blocked_code is ErrorCode.TOTAL_ABOVE_LIMIT

    def test_an_absent_order_total_limit_blocks_even_a_cheap_order(
        self, rules, checkout
    ) -> None:
        """The limit is required, not merely compared.

        Without this the check above would still pass by coincidence
        whenever the real total happened to be low, which is precisely the
        case a user with an incomplete rule would see in testing and take
        for a working configuration.
        """
        loose = PurchaseRules(**{**rules.__dict__, "max_order_total": None})
        report = GUARD.check_final(loose, _product_for(rules), checkout)
        assert status_of(report, CHECK_MAX_ORDER_TOTAL) is CheckStatus.FAIL
        assert not report.passed

    def test_a_summary_row_with_two_colons_still_reads_the_grand_total(self) -> None:
        """ATTACK 2 DEFEATED: the whole row reaches the ceiling parser.

        ``money.parse_money_ceiling`` promises that a mis-read can only ever
        block, but that promise only holds if it is shown the whole string.
        ``CheckoutManager._read_summary`` used to keep just the text after
        the **last** colon in the row (``text.rpartition(":")``), so a total
        row carrying a second colon -- fine print inside the grand total --
        yielded the *smaller* embedded number while the row was still
        classified as the order total.

        It now classifies on the label and parses the entire row, so the
        largest price in the row wins and the guard sees the real charge.
        """
        row_text = "Order total: $1,299.00 including estimated tax: $104.00"
        # The parser on its own is safe...
        assert parse_money_ceiling(row_text) == usd("1299.00")
        # ...and the reader no longer truncates before reaching it.
        reader = _reader_with_summary_rows([row_text])
        summary = CHECKOUT._read_summary(reader)
        assert summary["total"] == usd("1299.00")

        checkout = CheckoutSnapshot(
            lines=(
                CartLine(asin=ASIN, title="thing", quantity=1, unit_price=usd("1299.00")),
            ),
            order_total=summary["total"],
            address_label="John D., Raleigh, NC 27601",
            payment_label="Visa ending in 1234",
            place_order_control_found=True,
        )
        rules = PurchaseRules(
            expected_asin=ASIN,
            max_order_total=usd("150.00"),
            seller_policy=SellerPolicy.ANY,
            condition_policy=ConditionPolicy.NEW_ONLY,
            require_address_match=False,
            require_payment_match=False,
        )
        report = GUARD.check_final(rules, _product_for(rules), checkout)
        assert status_of(report, CHECK_MAX_ORDER_TOTAL) is CheckStatus.FAIL
        assert not report.passed  # $150 limit, $1,299 charge

    def test_a_missing_price_fraction_makes_the_price_unreadable(self) -> None:
        """ATTACK 2 DEFEATED: ``parse_split_price`` refuses to invent cents.

        ``money.py``'s contract is that every ambiguity resolves in the
        blocking direction. When the ``.a-price-fraction`` node cannot be
        read -- a one-selector failure, while ``.a-price-whole`` still
        matches and still carries its trailing separator -- assuming ``00``
        would under-read by up to 99c, in the spending direction, and
        ``ProductParser._read_price`` calls this whenever ``whole`` is truthy
        without requiring ``fraction``.

        So the trailing separator is treated as proof that a fraction existed
        and was lost: the result is ``None``, the price is absent, and
        ``_check_item_price_known`` blocks.
        """
        from app.core.money import parse_split_price

        assert parse_split_price("109.", "97") == usd("109.97")
        assert parse_split_price("109.", None) is None  # fraction lost, not 109.00
        # A whole part with no separator is unambiguous, so it still parses.
        assert parse_split_price("109", None) == usd("109.00")

        rules = PurchaseRules(
            expected_asin=ASIN,
            max_item_price=usd("109.00"),
            seller_policy=SellerPolicy.ANY,
        )
        unreadable = ProductSnapshot(
            asin=ASIN,
            price=parse_split_price("109.", None),
            availability=Availability.IN_STOCK,
            condition=ItemCondition.NEW,
            seller="Amazon.com",
        )
        report = GUARD.check_product(rules, unreadable)
        assert status_of(report, CHECK_ITEM_PRICE) is CheckStatus.FAIL
        assert not report.passed

    def test_an_unreadable_checkout_quantity_is_never_fabricated_as_one(self) -> None:
        """ATTACK 2 + 3 DEFEATED: an unread quantity is ``None``, and blocks.

        ``CheckoutManager._line_quantity`` tries a form control and then
        several wordings. When none of them answers it returns ``None``
        rather than ``1``: a fabricated ``1`` would be *validated* against
        ``rules.quantity == 1``, reported as a PASS, and the order would go
        through holding three of the item.

        ``_check_quantity_in_checkout`` therefore fails on ``quantity is
        None`` specifically, before it does any arithmetic -- ``CartLine.
        units`` would quietly read the same unknown as zero.
        """
        row = FakeLocator(
            text="Klein Tools CL800 Clamp Meter $109.97 three of them",
            attributes={"data-asin": ASIN},
            children={
                ".a-price": FakeLocator(text="$109.97"),
                ".sc-product-title": FakeLocator(text="Klein Tools CL800 Clamp Meter"),
            },
        )
        reader = _reader_with_line_items([row])
        lines = CHECKOUT._read_lines(reader)
        assert len(lines) == 1
        assert lines[0].quantity is None  # not 1

        rules = PurchaseRules(
            expected_asin=ASIN,
            quantity=1,
            max_item_price=usd("120.00"),
            max_order_total=usd("400.00"),
            seller_policy=SellerPolicy.ANY,
            require_address_match=False,
            require_payment_match=False,
        )
        checkout = CheckoutSnapshot(
            lines=tuple(lines),
            order_total=usd("329.91"),  # three of them
            address_label="John D., Raleigh, NC 27601",
            payment_label="Visa ending in 1234",
            place_order_control_found=True,
        )
        report = GUARD.check_final(rules, _product_for(rules), checkout)
        assert status_of(report, CHECK_QUANTITY) is CheckStatus.FAIL
        assert not report.passed  # never buys three believing it bought one

    def test_a_checkout_quantity_control_is_read_rather_than_the_wording(self) -> None:
        """The quantity reader really does read a ``<select>``.

        Without this the test above would also pass against a reader that
        answered ``None`` unconditionally, which would block every purchase
        instead of checking one.
        """
        row = FakeLocator(
            text="Klein Tools CL800 Clamp Meter $109.97",
            attributes={"data-asin": ASIN},
            children={
                "select[name": FakeLocator(attributes={"value": "3"}),
                ".a-price": FakeLocator(text="$109.97"),
            },
        )
        lines = CHECKOUT._read_lines(_reader_with_line_items([row]))
        assert lines[0].quantity == 3

        rules = PurchaseRules(expected_asin=ASIN, quantity=1)
        checkout = CheckoutSnapshot(
            lines=tuple(lines),
            order_total=usd("329.91"),
            place_order_control_found=True,
        )
        report = GUARD.check_final(rules, _product_for(rules), checkout)
        assert status_of(report, CHECK_QUANTITY) is CheckStatus.FAIL
        assert not report.passed

    def test_amazons_own_qty_wording_is_readable(self) -> None:
        """The wording patterns must actually match Amazon's own markup.

        This is the other half of the quantity protection: blocking on an
        unreadable quantity is only correct if a readable one *is* read.

        ``CHECKOUT_QUANTITY_PATTERNS`` once held a literal U+0008 BACKSPACE
        where the regex escape ``\\b`` was meant -- an invisible corruption in
        three of the four patterns, so none of them could match any text at
        all. ``Qty: 3``, the wording Amazon's classic checkout uses, read as
        unknown, and ``_check_quantity_in_checkout`` then refused every
        conforming order. It failed safe, but it failed always.

        The word-boundary escapes are checked directly as well as through the
        reader, because the corruption is not visible in a diff.
        """
        from app.automation import selectors

        for pattern in selectors.CHECKOUT_QUANTITY_PATTERNS:
            assert "\x08" not in pattern.pattern, repr(pattern.pattern)

        row = FakeLocator(
            text="Klein Tools CL800 Clamp Meter Qty: 3 $109.97",
            attributes={"data-asin": ASIN},
            children={".a-price": FakeLocator(text="$109.97")},
        )
        lines = CHECKOUT._read_lines(_reader_with_line_items([row]))
        assert lines[0].quantity == 3


# ---------------------------------------------------------------------------
# Attack 4 -- buying from a disallowed seller
# ---------------------------------------------------------------------------


class TestAttack4Sellers:
    def test_the_seller_on_the_order_line_is_checked_too(
        self, rules, snapshot
    ) -> None:
        """ATTACK 4 DEFEATED: PRE_SUBMIT checks the offer being bought.

        Every other seller check reads ``product``, the snapshot taken on the
        product page, and Amazon's buybox can change hands between that read
        and arriving at checkout. ``_check_checkout_seller`` is the only check
        that looks at ``CheckoutSnapshot``'s own line sellers, which
        ``CheckoutManager._line_quantity``'s neighbour ``_line_seller`` now
        populates from the "Sold by" text.

        The product-page check still passes here -- it is looking at stale
        data, which is exactly the point -- so the block comes entirely from
        the checkout-line check.
        """
        substituted = CheckoutSnapshot(
            lines=(
                CartLine(
                    asin=ASIN,
                    title="Klein Tools CL800 Clamp Meter",
                    quantity=1,
                    unit_price=usd("109.97"),
                    seller="Bargain Bin Electronics",
                ),
            ),
            order_total=usd("117.94"),
            address_label="John D., Raleigh, NC 27601",
            payment_label="Visa ending in 1234",
            place_order_control_found=True,
        )
        assert rules.seller_policy is SellerPolicy.AMAZON_ONLY
        assert not rules.seller_allowed("Bargain Bin Electronics")

        report = GUARD.check_final(rules, snapshot, substituted)
        assert status_of(report, CHECK_SELLER) is CheckStatus.PASS  # from the snapshot
        assert status_of(report, CHECK_CHECKOUT_SELLER) is CheckStatus.FAIL
        assert not report.passed
        assert report.blocked_code is ErrorCode.SELLER_NOT_ALLOWED

    def test_an_unverifiable_checkout_seller_does_not_satisfy_a_strict_rule(
        self, rules, snapshot, checkout
    ) -> None:
        """An absent "Sold by" is reported, never treated as a pass.

        Under a strict policy the check is SKIPPED with an explanation rather
        than PASS, so the guard table cannot show a green tick for a seller
        nobody read.
        """
        report = GUARD.check_final(rules, snapshot, checkout)
        assert status_of(report, CHECK_CHECKOUT_SELLER) is CheckStatus.SKIPPED
        assert report.passed  # the product-page seller carried the decision

        permissive = PurchaseRules(
            **{**rules.__dict__, "seller_policy": SellerPolicy.ANY}
        )
        relaxed = GUARD.check_final(permissive, snapshot, checkout)
        assert (
            status_of(relaxed, CHECK_CHECKOUT_SELLER) is CheckStatus.NOT_APPLICABLE
        )

    def test_unicode_compatibility_folding_cannot_impersonate_amazon_retail(
        self,
    ) -> None:
        """ATTACK 4 DEFEATED: a homoglyph seller name is refused.

        ``normalise_label`` applies NFKD, which folds mathematical and
        fullwidth letter variants onto ASCII -- so a marketplace storefront
        named with those code points would normalise to exactly
        ``amazon.com`` and match :data:`AMAZON_RETAIL_SELLERS`, which is an
        *exact* comparison and has no substring test to fall back on.

        ``is_amazon_retail`` therefore refuses any non-ASCII seller name
        outright, before normalisation can launder it. Amazon's own retail
        names contain no such characters, so this costs nothing. The folding
        itself is deliberately left in place -- it is what makes
        ``"Amazon.com "`` and ``"amazon.com"`` the same seller -- which is
        why the refusal has to happen on the raw string.
        """
        def restyle(text: str, upper: int, lower: int) -> str:
            out = []
            for char in text:
                if "A" <= char <= "Z":
                    out.append(chr(upper + ord(char) - ord("A")))
                elif "a" <= char <= "z":
                    out.append(chr(lower + ord(char) - ord("a")))
                else:
                    out.append(char)
            return "".join(out)

        assert not is_amazon_retail("Amazon.com Deals")  # the documented case
        for impostor in (
            restyle("Amazon.com", 0x1D400, 0x1D41A),  # MATHEMATICAL BOLD
            restyle("Amazon.com", 0xFF21, 0xFF41),  # FULLWIDTH
        ):
            # The folding hazard is real: normalisation alone would match.
            assert normalise_label(impostor) == "amazon.com"
            # ...but the raw name is refused before it gets that far.
            assert not is_amazon_retail(impostor), impostor
            strict = PurchaseRules(
                expected_asin=ASIN, seller_policy=SellerPolicy.AMAZON_ONLY
            )
            assert not strict.seller_allowed(impostor)
        # The plain-ASCII name still works, so the refusal is not a blanket one.
        assert is_amazon_retail("Amazon.com")

    def test_the_manufacturer_allowance_refuses_a_lookalike_storefront(self) -> None:
        """ATTACK 4 DEFEATED (narrowed): no bare prefix match.

        ``AMAZON_OR_MANUFACTURER`` used to allow any seller whose name merely
        *started* with the brand, so "Bargain Bin Discount Warehouse" counted
        as the manufacturer "Bargain Bin" -- a different seller, with
        different stock and different returns.

        A seller now has to equal the brand exactly, or the brand followed by
        one of a known set of storefront words. This matters most because
        ``brand`` is scraped, not confirmed by the user:
        ``ProductService.suggest_rules`` copies ``snapshot.brand`` and
        ``ProductRepository.upsert_from_snapshot`` refreshes it from the page
        on every watch check, so the narrower the match, the less the page
        can decide.
        """
        scraped = PurchaseRules(
            expected_asin=ASIN,
            seller_policy=SellerPolicy.AMAZON_OR_MANUFACTURER,
            brand="Bargain Bin",  # read from the byline at purchase time
        )
        assert scraped.seller_allowed("Bargain Bin")  # exactly the brand
        assert scraped.seller_allowed("Bargain Bin Store")  # a known storefront
        assert not scraped.seller_allowed("Bargain Bin Electronics")
        assert not scraped.seller_allowed("Bargain Bin Discount Warehouse")
        assert not scraped.seller_allowed("Bargain Binary")

        # With no brand recorded at all the allowance cannot widen anything.
        brandless = PurchaseRules(
            expected_asin=ASIN,
            seller_policy=SellerPolicy.AMAZON_OR_MANUFACTURER,
        )
        assert not brandless.seller_allowed("Bargain Bin")


# ---------------------------------------------------------------------------
# Attack 5 -- buying unrelated cart contents
# ---------------------------------------------------------------------------


class TestAttack5ForeignItems:
    def test_allow_addons_cannot_permit_an_unrelated_basket(self, rules) -> None:
        """ATTACK 5 DEFEATED: the foreign-line check is not switchable.

        ``allow_addons`` is labelled "allow extras Amazon adds" in the rules
        editor, and that is now all it does: ``_check_addons`` judges only the
        recognised protection plans in ``checkout.addons``. Unrelated *line
        items* are judged separately by ``_check_foreign_lines``, which is
        always required -- no setting authorises buying something the user did
        not choose, which is the whole point of cart isolation.

        ``_check_asin_in_checkout`` only requires the target ASIN to be
        present, so it is the foreign-line check alone that catches this.
        """
        permissive = PurchaseRules(
            **{**rules.__dict__, "allow_addons": True, "max_order_total": usd("2000.00")}
        )
        polluted = CheckoutSnapshot(
            lines=(
                CartLine(asin=ASIN, title="Clamp meter", quantity=1, unit_price=usd("109.97")),
                CartLine(asin="B0DOGFOOD01", title="Dog food, 40 lb", quantity=2, unit_price=usd("64.99")),
                CartLine(asin="B0TVSET0001", title="65\" television", quantity=1, unit_price=usd("899.00")),
            ),
            addons=("4-year protection plan",),
            order_total=usd("1138.95"),
            address_label=rules.expected_address_label,
            payment_label=rules.expected_payment_label,
            place_order_control_found=True,
        )
        report = GUARD.check_final(permissive, _product_for(rules), polluted)
        # The flag does what its label says...
        assert status_of(report, CHECK_ADDONS) is CheckStatus.PASS
        # ...and nothing more.
        assert status_of(report, CHECK_CART_CONTENTS) is CheckStatus.FAIL
        assert not report.passed
        assert report.blocked_code is ErrorCode.UNEXPECTED_CART_ITEMS

    def test_a_foreign_line_blocks_even_with_every_setting_relaxed(self) -> None:
        """No combination of user settings reaches a pass with a foreign line."""
        anything_goes = PurchaseRules(
            expected_asin=ASIN,
            max_item_price=None,
            max_order_total=usd("5000.00"),
            seller_policy=SellerPolicy.ANY,
            condition_policy=ConditionPolicy.ALLOW_USED,
            require_address_match=False,
            require_payment_match=False,
            allow_addons=True,
            allow_subscription=True,
        )
        polluted = CheckoutSnapshot(
            lines=(
                CartLine(asin=ASIN, title="Clamp meter", quantity=1, unit_price=usd("109.97")),
                CartLine(asin=None, title=None, quantity=1, unit_price=usd("19.99")),
            ),
            order_total=usd("129.96"),
            address_label="John D., Raleigh, NC 27601",
            payment_label="Visa ending in 1234",
            place_order_control_found=True,
        )
        report = GUARD.check_final(anything_goes, _product_for(anything_goes), polluted)
        assert status_of(report, CHECK_CART_CONTENTS) is CheckStatus.FAIL
        assert not report.passed

    def test_a_null_asin_cart_line_is_foreign(self, rules, snapshot) -> None:
        """ATTACK 5 DEFEATED at three independent points.

        ``foreign_lines`` treats a null ASIN as foreign -- an unidentifiable
        line is not the target item -- so ``prepare_purchase``'s inline check
        raises on it. ``GUARD.check_cart`` is the phase that catches the other
        shape of this: the target item already sitting in the cart in the
        wrong quantity, which is not foreign and so slips past a
        foreign-line test. That phase is now run in production (see
        ``TestPreCheckoutGuardPhase``).
        """
        from app.purchasing.models import CartState

        unidentifiable = CartState(
            lines=(
                CartLine(asin=ASIN, title="Clamp meter", quantity=1, unit_price=usd("109.97")),
                CartLine(asin=None, title=None, quantity=1, unit_price=usd("4.99")),
            ),
        )
        assert len(unidentifiable.foreign_lines(ASIN)) == 1
        assert not GUARD.check_cart(rules, snapshot, unidentifiable).passed

        already_there = CartState(
            lines=(
                CartLine(asin=ASIN, title="Clamp meter", quantity=3, unit_price=usd("109.97")),
            ),
            subtotal=usd("329.91"),
        )
        # No foreign lines, so plan_isolation picks EMPTY_CART and
        # prepare_purchase's inline check is satisfied...
        assert already_there.foreign_lines(ASIN) == ()
        # ...so the cart-quantity check in the PRE_CHECKOUT phase is the only
        # thing that catches it.
        cart_report = GUARD.check_cart(rules, snapshot, already_there)
        assert status_of(cart_report, CHECK_CART_QUANTITY) is CheckStatus.FAIL
        assert not cart_report.passed

    def test_a_page_text_timeout_cannot_make_a_full_cart_look_empty(self) -> None:
        """ATTACK 5 DEFEATED: ``reported_empty`` needs zero line rows.

        Two things used to combine badly here. ``PageReader.page_text`` fell
        back to ``page.content()`` -- the raw HTML -- when
        ``body.inner_text()`` timed out, which on a slow cart page is an
        ordinary outcome; and ``read_cart`` believed Amazon's empty-cart
        wording wherever it found it. Amazon's cart markup carries that
        wording in a template that is merely *hidden* when the cart is full,
        so a full cart read as ``CartState(lines=(), reported_empty=True)``,
        ``plan_isolation`` chose EMPTY_CART, and "Proceed to checkout" took
        the whole cart.

        Both halves are fixed: ``page_text`` returns "" rather than markup,
        and the rows are counted before any empty-cart signal is trusted. So
        even with the empty-cart element *matching* and the text unreadable,
        a cart with rows is parsed as a cart with rows.
        """
        from app.automation.cart_manager import MANAGER as CART
        from app.automation.page_reader import PageReader

        rows = FakeLocator(
            items=[
                FakeLocator(
                    text="Dog food",
                    attributes={"data-asin": "B0DOGFOOD01"},
                ),
                FakeLocator(
                    text="Kettle",
                    attributes={"data-asin": "B0KETTLE001"},
                ),
            ]
        )
        page = FakePage(
            {
                # Amazon's hidden empty-cart template, present and matching.
                ".sc-your-amazon-cart-is-empty": FakeLocator(count=1),
                # The active cart, holding two rows.
                "#sc-active-cart": FakeLocator(children={".sc-list-item": rows}),
                # body.inner_text() times out, as it does on a slow cart page.
                "body": FakeLocator(raises=True),
            },
            url="https://www.amazon.com/gp/cart/view.html",
        )
        reader = PageReader(page)
        assert reader.page_text() == ""  # no fall back to page.content()

        cart = CART.read_cart(reader)
        assert cart.reported_empty is False
        assert [line.asin for line in cart.lines] == ["B0DOGFOOD01", "B0KETTLE001"]
        assert not cart.is_empty

        product = ProductSnapshot(
            asin=ASIN,
            price=usd("109.97"),
            availability=Availability.IN_STOCK,
            condition=ItemCondition.NEW,
            seller="Amazon.com",
            buy_now_available=False,  # force the cart route
            add_to_cart_available=True,
        )
        plan = CART.plan_isolation(
            product=product,
            rules=PurchaseRules(expected_asin=ASIN),
            cart=cart,
            allow_set_aside=False,
        )
        from app.purchasing.models import CartStrategy

        # Not EMPTY_CART: the two foreign lines are seen, and with
        # set-aside refused the purchase stops rather than ordering them.
        assert plan.strategy is CartStrategy.BLOCKED
        assert plan.is_blocked


# ---------------------------------------------------------------------------
# Attack 3 / 9 -- variations and subscriptions read as "fine"
# ---------------------------------------------------------------------------


class TestAttack3And9SilentDegradation:
    def test_an_unknown_variation_label_becomes_no_expectation_at_all(self) -> None:
        """ATTACK 3 SUCCEEDS, partially: a dropped label is never reported.

        ``ProductParser._add_dimension`` discards any label outside
        ``KNOWN_VARIATION_LABELS``. If Amazon renames "Color" to something
        unlisted *before* the user sets the rule up, the recorded expectation
        is empty -- and an empty expectation makes ``_check_variation``
        report NOT_APPLICABLE or SKIPPED for every future observation, both
        of which are ``required=False`` and therefore pass.

        So the version is unconstrained for the life of the rule, and nothing
        tells the user their "Color: Black" choice was not recorded. This is
        an outstanding gap, pinned here so it is not mistaken for a
        protection: the fix belongs at rule-creation time (refuse to store an
        expectation that reads as empty when the page plainly has options),
        not in the guard, which can only see what was stored.
        """
        from app.automation.product_parser import PARSER

        found: dict[str, str] = {}
        PARSER._add_dimension(found, "Shade:", "Black")  # Amazon renamed "Color"
        assert found == {}

        expectation = VariationSnapshot(found)
        assert expectation.fingerprint == "none"
        rules = PurchaseRules(
            expected_asin=ASIN, expected_variation=expectation, seller_policy=SellerPolicy.ANY
        )
        # Any variation now satisfies the rule, including none at all.
        for observed in (VariationSnapshot(), VariationSnapshot({"Color": "Gold"})):
            report = GUARD.check_product(
                rules,
                ProductSnapshot(
                    asin=ASIN,
                    price=usd("109.97"),
                    availability=Availability.IN_STOCK,
                    condition=ItemCondition.NEW,
                    seller="Amazon.com",
                    variation=observed,
                ),
            )
            assert status_of(report, CHECK_VARIATION) in {
                CheckStatus.NOT_APPLICABLE,
                CheckStatus.SKIPPED,
            }
            assert report.passed

    def test_a_renamed_subscribe_and_save_row_is_caught_by_the_wording(
        self,
    ) -> None:
        """ATTACK 9 DEFEATED: the ids change, the words do not.

        ``read_subscription_preselected`` resolves "cannot tell" to True
        everywhere -- an unwanted recurring subscription is the expensive
        mistake -- but its first branch could not tell the difference between
        "no Subscribe & Save row exists" and "the row was renamed". It
        returned ``False`` for both, so a page offering a pre-selected
        Subscribe & Save under a new id read as a one-time purchase.

        The backstop is the buy box's own wording, scoped to the buy box
        because the same phrases appear in recommendation strips on pages
        with no subscription option at all. With the wording present and the
        row selectors stale, "not a subscription" may only be concluded from
        a one-time option that is *positively* selected -- and there is none
        here, so the guard blocks.
        """
        from app.automation.page_reader import PageReader
        from app.automation.product_parser import PARSER

        # A live S&S accordion, but under an id no candidate matches.
        page = FakePage(
            {
                "#snsAccordionRowRenamed": FakeLocator(
                    attributes={"class": "a-accordion-row a-accordion-active"}
                ),
                "#buybox": FakeLocator(
                    text="Subscribe & Save: deliver every 2 months  $29.97"
                ),
            }
        )
        assert PARSER.read_subscription_preselected(PageReader(page)) is True

        rules = PurchaseRules(expected_asin=ASIN, seller_policy=SellerPolicy.ANY)
        report = GUARD.check_product(
            rules,
            ProductSnapshot(
                asin=ASIN,
                price=usd("29.97"),
                availability=Availability.IN_STOCK,
                condition=ItemCondition.NEW,
                seller="Amazon.com",
                subscription_preselected=True,  # what the parser reported
            ),
        )
        assert not report.passed
        assert report.blocked_code is ErrorCode.SUBSCRIPTION_DETECTED

    def test_a_page_with_no_subscription_offer_reads_as_one_time(self) -> None:
        """The wording backstop must not block an ordinary purchase.

        The phrases are looked for inside the buy box only; a product page
        with no subscription option anywhere has to come back ``False``, or
        every purchase would be refused as a suspected subscription.
        """
        from app.automation.page_reader import PageReader
        from app.automation.product_parser import PARSER

        page = FakePage({"#buybox": FakeLocator(text="In Stock  $29.97  Buy Now")})
        assert PARSER.read_subscription_preselected(PageReader(page)) is False


# ---------------------------------------------------------------------------
# Structural: all three guard phases run in production
# ---------------------------------------------------------------------------


class TestPreCheckoutGuardPhase:
    def test_the_pre_checkout_phase_is_reached_from_the_purchase_service(self) -> None:
        """``docs/ARCHITECTURE.md`` claims three phases run. Three do.

        ``GUARD.check_cart`` -- the whole ``PRE_CHECKOUT`` phase, and the only
        home of ``cart_contents`` and ``cart_quantity`` -- was once referenced
        nowhere under ``app/``, which made the phase dead code and left the
        docs describing a protection that did not exist.

        It is now called from ``PurchaseService._guard_cart``, which is handed
        to ``ADAPTER.prepare_purchase`` as ``on_cart_read`` and invoked while
        the item is still only in a cart. Both halves are checked: a caller
        that no longer passed the callback would leave the phase dead again
        without removing the call.
        """
        from pathlib import Path

        app_dir = Path(__file__).resolve().parents[2] / "app"
        callers = sorted(
            path.name
            for path in app_dir.rglob("*.py")
            if "check_cart(" in path.read_text(encoding="utf-8")
            and path.name != "purchase_guard.py"
        )
        assert callers == ["purchase_service.py"]

        service_source = (
            app_dir / "purchasing" / "purchase_service.py"
        ).read_text(encoding="utf-8")
        assert "on_cart_read=" in service_source

    def test_a_wrong_cart_quantity_blocks_before_checkout_is_entered(
        self, service, snapshot, rules
    ) -> None:
        """The phase does real work: it stops a purchase the later phases miss.

        The target item is already in the cart three times. It is not a
        *foreign* line, so neither ``plan_isolation`` nor
        ``prepare_purchase``'s inline foreign-line check objects, and the
        checkout the fake adapter serves is the conforming one -- so
        ``check_final`` would pass. Only ``_check_cart_quantity``, in this
        phase, catches it, and it does so while the item is still in a cart
        and nothing has been ordered.
        """
        from dataclasses import replace

        from app.purchasing.models import CartState

        built, ctx = service
        adapter = ctx["adapter"]
        # No Buy Now, so the flow takes the cart route where the phase runs.
        adapter.snapshot = replace(
            snapshot, buy_now_available=False, add_to_cart_available=True
        )
        adapter.cart = CartState(
            lines=(
                CartLine(
                    asin=ASIN,
                    title="Klein Tools CL800 Clamp Meter",
                    quantity=3,
                    unit_price=usd("109.97"),
                ),
            ),
            subtotal=usd("329.91"),
        )

        job_id = built.start(
            product_id=ctx["product"].id, rules=rules, mode=PurchaseMode.AUTOMATIC
        )

        assert ctx["purchases"].get(job_id).state is PurchaseState.BLOCKED
        assert adapter.authorizations == []
        assert adapter.cart_reads_guarded == 1
        from app.purchasing.validation import GuardPhase

        report = ctx["purchases"].latest_guard_report(job_id, GuardPhase.PRE_CHECKOUT)
        assert report is not None and not report["passed"]


# ---------------------------------------------------------------------------
# Attacks 6, 7, 8 -- driven through the real PurchaseService
# ---------------------------------------------------------------------------


class FakeSession:
    """The slice of ``BrowserSession`` the purchase service touches."""

    def __init__(self) -> None:
        self.steps: list[str] = []

    def step(self, message: str) -> None:
        self.steps.append(message)

    def check_cancelled(self) -> None:
        return None

    @property
    def reader(self) -> Any:  # pragma: no cover - the fake adapter never reads
        raise AssertionError("no browser in these tests")


def _make_worker():
    """A worker that runs submitted work inline, on the calling thread.

    ``PurchaseService`` only needs ``submit``, ``cancel`` and the three Qt
    signals it connects to.
    """

    from PySide6.QtCore import QObject, Signal

    class _Worker(QObject):
        progress = Signal(int, str)
        finished = Signal(int, object, object)
        failed = Signal(int, object, object)
        cancelled = Signal(int, object)

        def __init__(self) -> None:
            super().__init__()
            self.calls: list[str] = []
            self.errors: list[BaseException] = []

        def submit(self, label, run, *, priority=None, context=None) -> int:
            self.calls.append(label)
            try:
                run(FakeSession())
            except BaseException as exc:  # noqa: BLE001 - recorded, not raised
                self.errors.append(exc)
            return len(self.calls)

        def cancel(self, task_id: int) -> None:
            return None

    return _Worker()


class FakeAdapter:
    """Answers the adapter calls ``PurchaseService`` makes, with no browser."""

    def __init__(self, *, snapshot, checkout, cart=None) -> None:
        from app.purchasing.models import CartState

        self.snapshot = snapshot
        self.checkout = checkout
        self.cart = cart if cart is not None else CartState(reported_empty=True)
        self.authorizations: list[Any] = []
        self.checkouts_read = 0
        #: How many times the pre-checkout guard callback was invoked.
        self.cart_reads_guarded = 0

    def inspect_product(self, session, *, asin, marketplace=None):
        return self.snapshot

    def read_cart(self, session):
        return self.cart

    def prepare_purchase(
        self, session, *, rules, plan, marketplace=None, journal=None,
        on_journal_change=None, on_cart_read=None,
    ):
        """Mirrors the real adapter, including the pre-checkout callback.

        ``on_cart_read`` is invoked on the cart route only -- the Buy Now
        route never touches the cart -- and it may raise to stop the
        purchase, which is how ``GuardPhase.PRE_CHECKOUT`` blocks.
        """
        from app.automation.amazon_adapter import PreparedPurchase
        from app.automation.cart_manager import IsolationJournal
        from app.purchasing.models import CartStrategy

        if plan.strategy is not CartStrategy.BUY_NOW and callable(on_cart_read):
            self.cart_reads_guarded += 1
            on_cart_read(self.snapshot, self.cart)

        return PreparedPurchase(
            product=self.snapshot,
            checkout=self.checkout,
            strategy=plan.strategy,
            journal=journal or IsolationJournal(strategy=plan.strategy),
        )

    def refresh_checkout(self, session):
        self.checkouts_read += 1
        return self.checkout

    def submit_order(self, session, authorization):
        """Stand in for the click, honouring the authorisation barrier."""
        from app.purchasing.models import OrderConfirmation

        authorization.validate()
        self.authorizations.append(authorization)
        authorization.record_submission()
        return OrderConfirmation(
            verified=True,
            order_number="112-1234567-7654321",
            order_total=self.checkout.order_total,
        )

    def restore_cart(self, session, journal):
        return 0


@pytest.fixture
def service(database, app_paths, monkeypatch, snapshot, checkout):
    """A real ``PurchaseService`` over real repositories and a fake browser."""
    from app.config import SettingsService
    from app.database.repositories import (
        ActivityRepository,
        OrderRepository,
        WatchRepository,
    )
    from app.purchasing import purchase_service as module

    settings = SettingsService(database)
    settings.reload()
    settings.update(test_mode=False, auto_buy_acknowledged=True)

    products = ProductRepository(database)
    product = products.upsert_from_snapshot(snapshot)

    adapter = FakeAdapter(snapshot=snapshot, checkout=checkout)
    monkeypatch.setattr(module, "ADAPTER", adapter)

    worker = _make_worker()
    built = module.PurchaseService(
        worker=worker,
        products=products,
        rules_repo=RulesRepository(database),
        purchases=PurchaseRepository(database),
        orders=OrderRepository(database),
        watches=WatchRepository(database),
        activity=ActivityRepository(database),
        settings=settings,
    )
    return built, {
        "product": product,
        "adapter": adapter,
        "worker": worker,
        "settings": settings,
        "purchases": PurchaseRepository(database),
        "products": products,
        "orders": OrderRepository(database),
        "rules_repo": RulesRepository(database),
    }


class TestAttack6TestMode:
    def test_turning_test_mode_on_mid_flight_stops_the_order(
        self, service, snapshot, rules
    ) -> None:
        """ATTACK 6 DEFEATED: test mode is read live, at the moment of submit.

        ``PurchaseService.start`` freezes ``settings.test_mode`` into the job
        row, which is right for deciding what kind of run this is, but it
        cannot be the only reading. The sequence that matters is:

        1. test mode is off; the user starts an assisted purchase;
        2. while the confirmation dialog is open they switch test mode on in
           Settings (nothing warns them a purchase is in flight);
        3. they press the confirm button.

        The user's latest instruction is "do not order", so ``_submit``
        re-reads ``self._settings.current.test_mode`` -- OR-ed with the job's
        own frozen flag, so neither reading can unset the other -- and
        cancels without ever building an authorisation. The barrier in
        ``CheckoutManager.submit`` could not have helped: it re-validates the
        authorisation it is given, and that authorisation used to be built
        with a hard-coded ``test_mode=False``.
        """
        built, ctx = service
        product = ctx["product"]
        job_id = built.start(
            product_id=product.id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        assert ctx["purchases"].get(job_id).state is PurchaseState.AWAITING_CONFIRMATION
        assert ctx["purchases"].get(job_id).test_mode is False

        # The user switches test mode on, believing it makes the app safe.
        ctx["settings"].update(test_mode=True)
        assert ctx["settings"].current.test_mode is True

        built.confirm(job_id)

        assert ctx["adapter"].authorizations == []  # nothing was authorised
        assert ctx["purchases"].get(job_id).state is PurchaseState.CANCELLED
        assert ctx["orders"].for_purchase_job(job_id) is None

    def test_leaving_test_mode_off_mid_flight_still_places_the_order(
        self, service, snapshot, rules
    ) -> None:
        """The live reading only ever stops an order, never invents one.

        Without this the test above would also pass against a ``_submit``
        that refused unconditionally, which would look safe and buy nothing.
        """
        built, ctx = service
        job_id = built.start(
            product_id=ctx["product"].id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        built.confirm(job_id)

        authorization = ctx["adapter"].authorizations[-1]
        assert authorization.test_mode is False
        assert ctx["purchases"].get(job_id).state is PurchaseState.CONFIRMED
        assert ctx["orders"].for_purchase_job(job_id) is not None

    def test_a_test_mode_job_still_cannot_reach_a_click(
        self, service, snapshot, rules
    ) -> None:
        """ATTACK 6 DEFEATED for a job that *starts* in test mode.

        ``_run_preparation`` diverts to ``_finish_test_run`` before any
        review is stored, so there is nothing for ``confirm()`` to act on.
        """
        built, ctx = service
        ctx["settings"].update(test_mode=True)
        product = ctx["product"]
        job_id = built.start(
            product_id=product.id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        assert ctx["purchases"].get(job_id).state is PurchaseState.CANCELLED
        assert ctx["adapter"].authorizations == []
        with pytest.raises(AppError):
            built.confirm(job_id)


class TestAttack7AutomaticMode:
    def test_automatic_mode_runs_the_same_final_guard_twice(
        self, service, snapshot, rules
    ) -> None:
        """ATTACK 7 DEFEATED: no weaker automatic path.

        Automatic mode reaches ``_submit`` through the same
        ``SubmitAuthorization``, and ``_submit`` re-reads the checkout and
        re-runs ``check_final`` plus ``verify_total_unchanged`` before
        building it. Two PRE_SUBMIT reports are persisted for one job.
        """
        built, ctx = service
        product = ctx["product"]
        job_id = built.start(
            product_id=product.id, rules=rules, mode=PurchaseMode.AUTOMATIC
        )
        assert ctx["purchases"].get(job_id).state is PurchaseState.CONFIRMED
        assert ctx["adapter"].checkouts_read == 1  # the re-read inside _submit
        from app.purchasing.validation import GuardPhase

        report = ctx["purchases"].latest_guard_report(job_id, GuardPhase.PRE_SUBMIT)
        assert report is not None and report["passed"]
        assert any(
            check["title"].startswith("Total unchanged") for check in report["checks"]
        )

    def test_automatic_mode_is_blocked_by_a_failing_final_check(
        self, service, snapshot, rules
    ) -> None:
        """The automatic path blocks on the same failures as the assisted one."""
        built, ctx = service
        ctx["adapter"].checkout = CheckoutSnapshot(
            lines=ctx["adapter"].checkout.lines,
            order_total=usd("999.00"),  # over the $135 limit
            address_label=rules.expected_address_label,
            payment_label=rules.expected_payment_label,
            place_order_control_found=True,
        )
        product = ctx["product"]
        job_id = built.start(
            product_id=product.id, rules=rules, mode=PurchaseMode.AUTOMATIC
        )
        assert ctx["purchases"].get(job_id).state is PurchaseState.BLOCKED
        assert ctx["adapter"].authorizations == []


class TestAttack8RetryAfterUncertainty:
    def test_an_uncertain_job_blocks_every_new_purchase_for_the_product(
        self, service, snapshot, rules
    ) -> None:
        """ATTACK 8 DEFEATED: UNKNOWN holds the product's only slot.

        ``UNKNOWN`` is a live state in both ``LIVE_STATES`` and the partial
        unique index, so ``start()`` refuses synchronously; the state table
        offers ``UNKNOWN -> {CONFIRMED, FAILED}`` only; and
        ``recover_interrupted`` never resumes a submission.
        """
        built, ctx = service
        product = ctx["product"]
        job_id = built.start(
            product_id=product.id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        built.confirm(job_id)
        purchases = ctx["purchases"]
        assert purchases.get(job_id).state is PurchaseState.CONFIRMED

        # A second job while the first is live is refused; prove it with the
        # uncertain state directly.
        other = ctx["products"].upsert_from_snapshot(
            ProductSnapshot(
                asin="B0OTHER0001",
                price=usd("10.00"),
                availability=Availability.IN_STOCK,
                condition=ItemCondition.NEW,
                seller="Amazon.com",
            )
        )
        stored = ctx["rules_repo"].create(PurchaseRules(expected_asin="B0OTHER0001"))
        first = purchases.create(
            product_id=other.id,
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
            PurchaseState.UNKNOWN,
        ):
            purchases.transition(first.id, step)
        with pytest.raises(AppError) as excinfo:
            purchases.create(
                product_id=other.id,
                rules_id=stored.rules_id,
                mode=PurchaseMode.AUTOMATIC,
                test_mode=False,
            )
        assert excinfo.value.code is ErrorCode.DUPLICATE_BLOCKED
        with pytest.raises(AppError):
            built.start(
                product_id=other.id,
                rules=PurchaseRules(expected_asin="B0OTHER0001"),
                mode=PurchaseMode.ASSISTED,
            )

    def test_confirming_twice_is_refused(self, service, snapshot, rules) -> None:
        """ATTACK 1 DEFEATED: a double-click on Place Order.

        ``confirm()`` pops the pending review, so the second call has nothing
        to act on -- and even if it did, the job has left
        ``AWAITING_CONFIRMATION`` and ``has_submitted`` is true.
        """
        built, ctx = service
        product = ctx["product"]
        job_id = built.start(
            product_id=product.id, rules=rules, mode=PurchaseMode.ASSISTED
        )
        built.confirm(job_id)
        with pytest.raises(AppError):
            built.confirm(job_id)
        assert len(ctx["adapter"].authorizations) == 1


# ---------------------------------------------------------------------------
# Helpers that need the fakes above
# ---------------------------------------------------------------------------


def _product_for(rules: PurchaseRules) -> ProductSnapshot:
    """A product snapshot that satisfies every product-level check."""
    return ProductSnapshot(
        asin=rules.expected_asin,
        price=rules.max_item_price or usd("1.00"),
        availability=Availability.IN_STOCK,
        availability_text="In Stock",
        seller=rules.expected_seller or "Amazon.com",
        ships_from=rules.expected_ships_from,
        condition=ItemCondition.NEW,
        variation=rules.expected_variation,
        prime_eligible=True,
    )


def _reader_with_summary_rows(texts: list[str]):
    from app.automation.page_reader import PageReader
    from app.automation import selectors

    rows = FakeLocator(items=[FakeLocator(text=text) for text in texts])
    return PageReader(FakePage({selectors.CHECKOUT_SUMMARY_ROWS.split(",")[0]: rows}))


def _reader_with_line_items(rows: list[FakeLocator]):
    from app.automation.page_reader import PageReader
    from app.automation import selectors

    holder = FakeLocator(items=rows)
    return PageReader(
        FakePage({selectors.CHECKOUT_LINE_ITEMS.split(",")[0].strip(): holder})
    )
