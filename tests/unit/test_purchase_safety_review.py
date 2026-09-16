"""Adversarial purchase-safety review.

Each test here is an *attack*: it tries to get a wrong, duplicate or
over-budget order authorised through the real code paths. A test that passes
in this file is a demonstration that the attack succeeds -- these are written
as assertions about the current (broken) behaviour, so that fixing the
underlying issue will make the test fail and force it to be rewritten as a
regression test.

Every test name says which of the nine attacks it belongs to.
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
    ) -> None:
        self._text = text
        self._count = count
        self._attributes = attributes or {}
        self._children = children or {}
        self._items = items or []

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

    def __init__(self, mapping: dict[str, FakeLocator], url: str = "https://x/") -> None:
        self._mapping = mapping
        self.url = url

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
        return "<html></html>"

    def wait_for_timeout(self, ms: float) -> None:
        return None


# ---------------------------------------------------------------------------
# Attack 1 -- a duplicate order
# ---------------------------------------------------------------------------


class TestAttack1Duplicates:
    def test_a_recorded_submission_can_be_recovered_to_a_terminal_state(
        self, purchases, products, rules_repo, snapshot, rules
    ) -> None:
        """ATTACK 1 SUCCEEDS: crash between record_submission() and the click.

        ``mark_submitted`` is only permitted from ``FINAL_VALIDATION`` or
        ``AWAITING_CONFIRMATION`` (``SUBMIT_ENTRY_STATES``), so the state at
        the moment the submission is recorded is one of those two. If the
        process dies there -- which is exactly the window the
        "record before the click" ordering exists to cover -- startup
        recovery moves the job to ``FAILED``:

        * ``FAILED`` is terminal, so the product's purchase slot is freed;
        * ``recover_interrupted`` does not return the id, so
          ``recover_after_restart`` never tells the user;
        * a second purchase for the same product is then accepted.

        The comment in ``recover_interrupted`` says such a job should be
        treated "as uncertain instead"; the code writes ``FAILED``.
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
        assert recovered.state is PurchaseState.FAILED
        assert recovered.state.is_terminal
        # Nothing tells the user an order may exist.
        assert uncertain == []
        assert not recovered.state.order_may_exist
        # And the product is free again, so a second order can be placed.
        assert purchases.live_job_for_product(product.id) is None
        second = purchases.create(
            product_id=product.id,
            rules_id=job.rules_id,
            mode=PurchaseMode.AUTOMATIC,
            test_mode=False,
        )
        assert second.id != job.id

    def test_recording_a_submission_pre_transition_is_a_supported_call(
        self, purchases, products, rules_repo, snapshot, rules
    ) -> None:
        """Why the recovery gap above is reachable, not theoretical.

        ``SUBMIT_RECORD_STATES`` deliberately permits recording a submission
        from ``FINAL_VALIDATION`` and ``AWAITING_CONFIRMATION`` as well as
        ``SUBMITTING``. So "submitted attempt while the job is still in a
        pre-submit state" is a state the repository's own contract allows --
        and it is the only one ``recover_interrupted`` mishandles.
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

        purchases.recover_interrupted()
        # 'submitting' and 'confirming' become UNKNOWN; this one does not.
        assert purchases.get(job.id).state is PurchaseState.FAILED


# ---------------------------------------------------------------------------
# Attack 2 -- buying above a limit
# ---------------------------------------------------------------------------


class TestAttack2OverBudget:
    def test_an_item_price_limit_alone_leaves_the_order_total_unbounded(
        self, rules, checkout
    ) -> None:
        """ATTACK 2 SUCCEEDS: ``max_order_total=None`` is a SKIPPED check.

        A skipped check is ``required=False`` and therefore passes, so an
        order with a $2,000 total is authorised against a $120 item limit.
        Nothing in ``PurchaseRules``, the repository schema or the rules
        editor requires an order-total limit to exist.
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
        assert status_of(report, CHECK_MAX_ORDER_TOTAL) is CheckStatus.SKIPPED
        assert report.passed  # authorised to spend $1,999.97

    def test_a_summary_row_with_two_colons_under_reads_the_order_total(self) -> None:
        """ATTACK 2 SUCCEEDS: the ceiling guarantee is defeated by truncation.

        ``money.parse_money_ceiling`` promises that a mis-read can only ever
        block. ``CheckoutManager._read_summary`` breaks that promise before
        the parser is reached: it keeps only the text after the **last**
        colon in the row (``text.rpartition(":")``). A total row that carries
        a second colon therefore yields the *smaller* embedded number.
        """
        row_text = "Order total: $1,299.00 including estimated tax: $104.00"
        # The parser on its own is safe...
        assert parse_money_ceiling(row_text) == usd("1299.00")
        # ...but the reader truncates first.
        reader = _reader_with_summary_rows([row_text])
        summary = CHECKOUT._read_summary(reader)
        assert summary["total"] == usd("104.00")

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
        assert status_of(report, CHECK_MAX_ORDER_TOTAL) is CheckStatus.PASS
        assert report.passed  # $150 limit, $1,299 charge

    def test_a_checkout_quantity_that_is_not_worded_qty_reads_as_one(self) -> None:
        """ATTACK 2 + 3 SUCCEED: quantity silently defaults to 1.

        ``CheckoutManager._read_lines`` has exactly one quantity reader, the
        regex ``qty\\s*:?\\s*(\\d+)``, and falls back to ``1`` whenever it
        does not match -- including when Amazon words the control
        "Quantity: 3" or renders it as a ``<select>``. The guard then
        compares the fabricated 1 against ``rules.quantity == 1`` and passes.

        ``GUARD.check_cart``, which holds the second quantity check, is never
        called by the application (see ``TestUnexercisedGuardPhase``), so
        this is the only quantity check in the production flow.
        """
        row = FakeLocator(
            text="Klein Tools CL800 Clamp Meter $109.97 Quantity: 3",
            attributes={"data-asin": ASIN},
            children={
                ".a-price": FakeLocator(text="$109.97"),
                ".sc-product-title": FakeLocator(text="Klein Tools CL800 Clamp Meter"),
            },
        )
        reader = _reader_with_line_items([row])
        lines = CHECKOUT._read_lines(reader)
        assert len(lines) == 1
        assert lines[0].quantity == 1  # the page said 3

        rules = PurchaseRules(
            expected_asin=ASIN,
            quantity=1,
            max_item_price=usd("120.00"),
            max_order_total=None,
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
        assert status_of(report, CHECK_QUANTITY) is CheckStatus.PASS
        assert report.passed  # buys three, believing it bought one


# ---------------------------------------------------------------------------
# Attack 4 -- buying from a disallowed seller
# ---------------------------------------------------------------------------


class TestAttack4Sellers:
    def test_the_seller_is_never_checked_against_the_actual_order(
        self, rules, snapshot
    ) -> None:
        """ATTACK 4 SUCCEEDS: PRE_SUBMIT re-checks a stale product snapshot.

        ``check_final`` passes ``product`` (read on the product page) to
        ``_check_seller``. ``CheckoutSnapshot`` lines carry a ``seller``
        field, and ``CartState`` lines are populated with it, but no guard
        check ever reads it -- and ``CheckoutManager._read_lines`` never even
        populates it. So a buybox flip between reading the product page and
        arriving at checkout is invisible: the offer that is actually bought
        is never attributed to a seller.
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
        assert report.passed

    def test_unicode_compatibility_folding_impersonates_amazon_retail(self) -> None:
        """ATTACK 4 SUCCEEDS: a homoglyph seller name passes ``is_amazon_retail``.

        ``normalise_label`` applies NFKD, which folds mathematical and
        fullwidth letter variants onto ASCII. A marketplace seller named with
        those code points satisfies an "Amazon only" rule exactly.
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
            assert normalise_label(impostor) == "amazon.com"
            assert is_amazon_retail(impostor), impostor
            strict = PurchaseRules(
                expected_asin=ASIN, seller_policy=SellerPolicy.AMAZON_ONLY
            )
            assert strict.seller_allowed(impostor)

    def test_the_manufacturer_allowance_trusts_a_page_supplied_brand(self) -> None:
        """ATTACK 4 SUCCEEDS: ``brand`` is scraped, not confirmed by the user.

        With ``AMAZON_OR_MANUFACTURER``, any seller whose name starts with
        the brand is allowed. ``brand`` comes from the product page byline:
        ``ProductService.suggest_rules`` copies ``snapshot.brand``, and
        ``PurchaseService.start`` falls back to ``product.brand``, which
        ``ProductRepository.upsert_from_snapshot`` refreshes from the page on
        every watch check. If the byline was unreadable at setup, whoever
        controls the page later decides who counts as the manufacturer.
        """
        scraped = PurchaseRules(
            expected_asin=ASIN,
            seller_policy=SellerPolicy.AMAZON_OR_MANUFACTURER,
            brand="Bargain Bin",  # read from the byline at purchase time
        )
        assert scraped.seller_allowed("Bargain Bin Electronics")
        assert scraped.seller_allowed("Bargain Bin")


# ---------------------------------------------------------------------------
# Attack 5 -- buying unrelated cart contents
# ---------------------------------------------------------------------------


class TestAttack5ForeignItems:
    def test_allow_addons_permits_an_entire_unrelated_basket(self, rules) -> None:
        """ATTACK 5 SUCCEEDS: one benign-sounding checkbox disables the check.

        ``_check_addons`` folds ``checkout.foreign_lines`` in with the
        recognised protection plans, and ``rules.allow_addons`` makes the
        whole thing pass. ``_check_asin_in_checkout`` only requires the target
        ASIN to be *present*. The rules editor presents the flag as "allow
        extras Amazon adds", but it also authorises the user's groceries.
        """
        permissive = PurchaseRules(
            **{**rules.__dict__, "allow_addons": True, "max_order_total": None}
        )
        polluted = CheckoutSnapshot(
            lines=(
                CartLine(asin=ASIN, title="Clamp meter", quantity=1, unit_price=usd("109.97")),
                CartLine(asin="B0DOGFOOD01", title="Dog food, 40 lb", quantity=2, unit_price=usd("64.99")),
                CartLine(asin="B0TVSET0001", title="65\" television", quantity=1, unit_price=usd("899.00")),
            ),
            order_total=usd("1138.95"),
            address_label=rules.expected_address_label,
            payment_label=rules.expected_payment_label,
            place_order_control_found=True,
        )
        report = GUARD.check_final(permissive, _product_for(rules), polluted)
        assert status_of(report, CHECK_ADDONS) is CheckStatus.PASS
        assert report.passed

    def test_a_null_asin_cart_line_is_foreign_but_never_reaches_the_guard(
        self, rules, snapshot
    ) -> None:
        """ATTACK 5 partially defeated, and where the remaining gap is.

        ``foreign_lines`` correctly treats a null ASIN as foreign, and
        ``prepare_purchase`` raises on any foreign cart line. But that inline
        check is the *only* cart check in the flow: ``GUARD.check_cart`` is
        never called, so ``_check_cart_quantity`` -- the check that would
        catch the target item already sitting in the cart -- never runs.
        """
        from app.purchasing.models import CartState

        already_there = CartState(
            lines=(
                CartLine(asin=ASIN, title="Clamp meter", quantity=3, unit_price=usd("109.97")),
            ),
            subtotal=usd("329.91"),
        )
        # No foreign lines, so plan_isolation picks EMPTY_CART and
        # prepare_purchase's inline check is satisfied...
        assert already_there.foreign_lines(ASIN) == ()
        # ...while the guard phase that would have caught the quantity is the
        # one the application never runs.
        cart_report = GUARD.check_cart(rules, snapshot, already_there)
        assert not cart_report.passed  # would have blocked, if it were called


# ---------------------------------------------------------------------------
# Attack 3 / 9 -- variations and subscriptions that degrade to "fine"
# ---------------------------------------------------------------------------


class TestAttack3And9SilentDegradation:
    def test_an_unknown_variation_label_becomes_no_expectation_at_all(self) -> None:
        """ATTACK 3, partial: an unrecognised label is dropped, not reported.

        ``ProductParser._add_dimension`` discards any label outside
        ``KNOWN_VARIATION_LABELS``. If Amazon renames "Color" to something
        unlisted *before* the user sets the rule up, the recorded expectation
        is empty, and ``_check_variation`` then reports NOT_APPLICABLE for
        every future observation -- the version is unconstrained for the life
        of the rule, with no sign to the user.
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

    def test_a_renamed_subscribe_and_save_row_reads_as_one_time(self) -> None:
        """ATTACK 9 SUCCEEDS (markup-dependent): absent selector -> False.

        ``read_subscription_preselected`` resolves "cannot tell" to True
        everywhere except the very first branch: if the
        ``SUBSCRIBE_AND_SAVE_ROW`` chain matches nothing it returns ``False``
        -- "no subscription" -- which is indistinguishable from "the selector
        broke". A page that offers a pre-selected Subscribe & Save under a
        renamed id is read as a one-time purchase, and
        ``_check_subscription`` passes.
        """
        from app.automation.page_reader import PageReader
        from app.automation.product_parser import PARSER

        # A live S&S accordion, but under an id no candidate matches.
        page = FakePage(
            {
                "#snsAccordionRowRenamed": FakeLocator(
                    attributes={"class": "a-accordion-row a-accordion-active"}
                )
            }
        )
        reader = PageReader(page)
        assert PARSER.read_subscription_preselected(reader) is False

        rules = PurchaseRules(expected_asin=ASIN, seller_policy=SellerPolicy.ANY)
        report = GUARD.check_product(
            rules,
            ProductSnapshot(
                asin=ASIN,
                price=usd("29.97"),
                availability=Availability.IN_STOCK,
                condition=ItemCondition.NEW,
                seller="Amazon.com",
                subscription_preselected=False,  # what the parser reported
            ),
        )
        assert report.passed


# ---------------------------------------------------------------------------
# Structural: the guard phase the application never runs
# ---------------------------------------------------------------------------


class TestUnexercisedGuardPhase:
    def test_no_application_code_calls_the_pre_checkout_phase(self) -> None:
        """``docs/ARCHITECTURE.md`` claims three phases run. Two do.

        ``GUARD.check_cart`` -- the whole ``PRE_CHECKOUT`` phase, and the only
        home of ``cart_contents`` and ``cart_quantity`` -- is referenced
        nowhere under ``app/``.
        """
        from pathlib import Path

        app_dir = Path(__file__).resolve().parents[2] / "app"
        callers = [
            path
            for path in app_dir.rglob("*.py")
            if "check_cart(" in path.read_text(encoding="utf-8")
            and path.name != "purchase_guard.py"
        ]
        assert callers == []


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
