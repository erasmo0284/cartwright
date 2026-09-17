"""Cart and checkout tests against real Chromium and fixture pages.

The tests in ``TestSubmitBarriers`` are the most important in the suite: they
assert that the only code path capable of placing an order refuses to do so
in test mode, without a passing guard, or when the total has moved.
``TestTurboCheckout`` asserts the same barriers again for Buy Now, where the
order button lives inside an iframe rather than on the page, and records what
the checkout reader can and cannot see from outside that frame.
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
from app.purchasing.purchase_guard import CHECK_CART_QUANTITY, GUARD
from app.purchasing.validation import CheckStatus
from tests.fixtures import amazon_pages as pages

pytestmark = pytest.mark.integration

ASIN = pages.DEFAULT_ASIN
CART_URL = "https://www.amazon.com/gp/cart/view.html"
CHECKOUT_URL = "https://www.amazon.com/gp/buy/spc/handlers/display.html"
#: Buy Now keeps the shopper on the product page and draws the modal over it,
#: so the Turbo host page is a ``/dp/`` URL rather than a checkout one.
TURBO_URL = f"https://www.amazon.com/dp/{ASIN}"
#: The modal's submit control, addressed the way the selectors address it.
TURBO_BUTTON = "#turbo-checkout-pyo-button"


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

    def test_an_unreadable_cart_quantity_is_never_fabricated_as_one(
        self, load, product, rules
    ) -> None:
        """A quantity nobody could read must not be reported as a PASS.

        This reader used to return 1 whenever the control was missing, on the
        reasoning that the guard would compare it against the expectation.
        That holds only when the user asked for a different number: against
        the common rule "quantity 1", a fabricated 1 is a PASS on a number
        that was never observed -- in the phase that now runs while the item
        is sitting in the cart.
        """
        html = pages.cart_with_target_only(quantity=1).replace(
            '<input name="quantityBox" value="1" type="text">', ""
        )
        reader = load(CART_URL, html)
        cart = CART.read_cart(reader)

        assert len(cart.lines) == 1
        assert cart.lines[0].quantity is None, "an unread quantity is not 1"
        assert cart.lines[0].line_price is None

        report = GUARD.check_cart(rules, product, cart)
        assert not report.passed
        quantity_check = next(
            check for check in report.checks if check.check_id == CHECK_CART_QUANTITY
        )
        assert quantity_check.status is CheckStatus.FAIL

    def test_a_worded_cart_quantity_is_read(self, load) -> None:
        """Amazon's compact cart renders "Qty: 2" as text, with no control.

        Without this fallback, refusing to fabricate a quantity would block
        every purchase from that layout.
        """
        html = pages.cart_with_target_only(quantity=2).replace(
            '<input name="quantityBox" value="2" type="text">',
            '<span class="sc-quantity-display">Qty: 2</span>',
        )
        reader = load(CART_URL, html)
        cart = CART.read_cart(reader)
        assert cart.lines[0].quantity == 2

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
        # The exposed binding resolves asynchronously, so the listener awaits
        # it and only then sets the flag the test waits on. Asserting
        # straight after submit() would race the binding and fail at random.
        page.evaluate(
            """
            const button = document.querySelector("input[name='placeYourOrder1']");
            button.addEventListener('click', async (event) => {
                event.preventDefault();
                await window.__noteClick();
                window.__clickNoted = true;
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
        page.wait_for_function("window.__clickNoted === true", timeout=10_000)
        assert order == ["recorded", "clicked"]

    def test_a_raising_recorder_prevents_the_click(self, load, page, site) -> None:
        """If the submission cannot be recorded, nothing is clicked."""
        clicks: list[str] = []
        site.add("gp/buy/spc", pages.checkout_page())
        page.goto(CHECKOUT_URL, wait_until="domcontentloaded")
        page.expose_function("__noteClick", lambda: clicks.append("clicked"))
        # ``window.__clicked`` is set synchronously inside the listener, so
        # reading it afterwards cannot race the exposed binding: if the
        # button had been clicked at all, the flag would already be true.
        page.evaluate(
            """
            document.querySelector("input[name='placeYourOrder1']")
              .addEventListener('click', (e) => {
                  e.preventDefault();
                  window.__clicked = true;
                  window.__noteClick();
              });
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
        assert page.evaluate("window.__clicked === true") is False
        assert clicks == []


def turbo_frame(page: object) -> object:
    """The Buy Now modal's frame object, once its document has loaded.

    ``page.frame_locator`` is enough to *query* the frame but hands back no
    handle for running script inside it, and instrumenting the real button with
    a click listener is the only way these tests can tell a refusal apart from
    a click that merely failed. The wait is on the button rather than on the
    panel so that a frame deliberately served without a panel can still be
    inspected.
    """
    page.frame_locator("#turbo-checkout-iframe").locator(TURBO_BUTTON).first.wait_for(
        state="attached", timeout=10_000
    )
    for frame in page.frames:
        if "turbo-checkout-iframe" in frame.url:
            return frame
    raise AssertionError("the Buy Now modal's frame never attached to the page")


def watch_turbo_button(page: object, frame: object, clicks: list[str]) -> None:
    """Record any click on the modal's order button, in the page and in Python.

    ``window.__clicked`` is set synchronously inside the listener, so reading
    it after a refusal cannot race the exposed binding: had the button been
    clicked at all, the flag would already be true.
    """
    page.expose_function("__noteClick", lambda: clicks.append("clicked"))
    frame.evaluate(
        """
        document.querySelector('#turbo-checkout-pyo-button')
          .addEventListener('click', (event) => {
              event.preventDefault();
              window.__clicked = true;
              window.__noteClick();
          });
        """
    )


class TestTurboCheckout:
    """The Buy Now modal, where the whole checkout lives inside an iframe.

    This is the path a purchase takes whenever Amazon offers Buy Now, which is
    the preferred cart-isolation strategy, so it is the path most real orders
    would go through. The tests below drive the real
    :class:`~app.automation.checkout_manager.CheckoutManager` against a real
    frame: a fixture that only pretended to be one would prove nothing, because
    the entire question is whether a locator built against the host document
    can reach into a second document.
    """

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

    def _load_turbo(self, load, **page_options: object) -> object:
        """Serve the host page and the modal's own document together.

        ``frame=`` carries options through to the iframe's own builder; every
        other keyword shapes the page that hosts it.
        """
        frame_options = page_options.pop("frame", {})
        return load(
            TURBO_URL,
            pages.turbo_checkout_page(**page_options),  # type: ignore[arg-type]
            also={
                pages.TURBO_IFRAME_URL: pages.turbo_checkout_frame(
                    **frame_options  # type: ignore[arg-type]
                )
            },
        )

    # ---- finding the button ---------------------------------------------

    def test_the_order_button_is_found_inside_the_frame(
        self, load, page, site
    ) -> None:
        """The button exists only in the modal, so only a frame search finds it.

        If this regressed the manager would fall through to the classic chain,
        find nothing on the host document, and report that Amazon's order
        button had disappeared -- turning every Buy Now purchase into a
        CHECKOUT_CHANGED failure.
        """
        reader = self._load_turbo(load)
        control = CHECKOUT.find_place_order(reader)

        assert control is not None, "the modal's order button was not found"
        assert control.in_turbo_frame is True, (
            "the button was found, but not reported as being in the Buy Now frame"
        )
        assert control.matches == 1
        assert control.locator.is_visible()
        assert page.locator(TURBO_BUTTON).count() == 0, (
            "the button must be unreachable from the host document, or this "
            "test would pass without the frame search working at all"
        )
        # The modal is a second document fetched over the wire; this proves it
        # came from the fixture site rather than from an unrouted 404 body that
        # happened to satisfy nothing.
        site.assert_no_unexpected_requests()

    def test_a_frame_whose_panel_never_renders_falls_back_to_the_main_page(
        self, load
    ) -> None:
        """A modal that never finished rendering must not strand the purchase.

        The frame here does contain an order button; only the panel container
        is missing. Clicking that button would submit against a half-rendered
        modal, which is why the panel -- not the button -- is the precondition,
        and why the fall-back to the host document is the right answer.

        This test pays the real wait: twenty seconds for each of the two panel
        candidates, forty in total, every time ``find_place_order`` is called
        against a stalled modal. That cost is deliberate here -- shortening it
        would stop the test proving what the application actually does.
        """
        reader = self._load_turbo(
            load,
            host_summary=True,
            host_place_order_button=True,
            frame={"panel": False},
        )
        control = CHECKOUT.find_place_order(reader)

        assert control is not None, "the host page's own order button was ignored"
        assert control.in_turbo_frame is False, (
            "a frame without a panel must not be treated as a usable modal"
        )

    def test_a_frame_without_a_panel_and_no_other_button_returns_none(
        self, load
    ) -> None:
        """Nothing to click is reported as nothing to click, not as an error.

        ``find_place_order`` is called speculatively -- by test mode, and once
        per step while advancing the checkout -- so raising here would turn an
        ordinary "not ready yet" into a failed purchase.
        """
        reader = self._load_turbo(load, frame={"panel": False})
        assert CHECKOUT.find_place_order(reader) is None

    # ---- reading the modal ------------------------------------------------

    def test_the_checkout_inside_the_frame_is_read_through_the_frame(
        self, load, product, rules
    ) -> None:
        """The whole order review lives in the modal, and is read from it.

        ``read_checkout`` used to read through a :class:`PageReader` bound to
        the host document while every money-bearing field sat in the frame, so
        the snapshot came back with no total, no address, no payment and no
        lines. That refused safely -- and refused *every* Buy Now purchase,
        which is the preferred cart-isolation strategy. The reader now
        resolves the frame once and scopes every read to it.
        """
        reader = self._load_turbo(load)
        snapshot = CHECKOUT.read_checkout(reader)

        assert snapshot.place_order_control_found is True
        assert snapshot.order_total == usd("117.94"), "the total is in the frame"
        assert snapshot.item_subtotal == usd("109.97")
        assert snapshot.tax == usd("7.97")
        assert snapshot.shipping == usd("0.00")
        assert snapshot.address_label is not None
        assert "Raleigh" in snapshot.address_label
        assert snapshot.payment_label == "Visa ending in 1234"
        assert len(snapshot.lines) == 1
        line = snapshot.lines[0]
        assert line.asin == ASIN
        assert line.quantity == 1

        # And the guard can now do its job on a Buy Now order rather than
        # refusing for want of anything to check.
        report = GUARD.check_final(rules, product, snapshot)
        assert report.passed, report.summary

    def test_a_modal_that_renders_no_summary_is_still_refused(
        self, load, product, rules
    ) -> None:
        """Reading the frame must not become "assume the frame is fine".

        With the panel present but the summary absent, there is no total to
        confirm, and an absent total has to fail rather than default.
        """
        reader = self._load_turbo(load, frame={"summary": False})
        snapshot = CHECKOUT.read_checkout(reader)

        assert snapshot.order_total is None
        report = GUARD.check_final(rules, product, snapshot)
        assert not report.passed, "a checkout with no total must never pass"

    # ---- the submit barriers, inside the frame ---------------------------
    # ---- the submit barriers, inside the frame ---------------------------

    def test_test_mode_cannot_submit_through_the_frame(self, load, page) -> None:
        """Test mode must not click the modal's button any more than the page's."""
        reader = self._load_turbo(load, host_summary=True)
        frame = turbo_frame(page)
        clicks: list[str] = []
        watch_turbo_button(page, frame, clicks)

        authorization = self._authorization(test_mode=True)
        with pytest.raises(AppError) as excinfo:
            CHECKOUT.submit(reader, authorization)

        assert excinfo.value.code is ErrorCode.INTERNAL_ERROR
        assert "Test mode" in excinfo.value.detail
        assert authorization.__dict__["_calls"] == []
        assert frame.evaluate("window.__clicked === true") is False
        assert clicks == []

    def test_a_failed_guard_cannot_submit_through_the_frame(self, load, page) -> None:
        """The guard gates the modal's button too, not only the classic page."""
        reader = self._load_turbo(load, host_summary=True)
        frame = turbo_frame(page)
        clicks: list[str] = []
        watch_turbo_button(page, frame, clicks)

        authorization = self._authorization(guard_passed=False)
        with pytest.raises(AppError):
            CHECKOUT.submit(reader, authorization)

        assert authorization.__dict__["_calls"] == []
        assert frame.evaluate("window.__clicked === true") is False
        assert clicks == []

    def test_a_changed_total_cannot_submit_through_the_frame(
        self, load, page
    ) -> None:
        """Consent was given for a specific amount, whichever button bears it.

        The summary is where Amazon puts it -- inside the modal -- and the
        total there differs from the one that was approved, so the frame's own
        button must not be clicked.
        """
        reader = self._load_turbo(
            load, frame={"order_total": "125.00", "tax": "15.03"}
        )
        frame = turbo_frame(page)
        clicks: list[str] = []
        watch_turbo_button(page, frame, clicks)

        authorization = self._authorization(approved_total=usd("117.94"))
        with pytest.raises(AppError) as excinfo:
            CHECKOUT.submit(reader, authorization)

        assert excinfo.value.code is ErrorCode.CHECKOUT_TOTAL_CHANGED
        assert authorization.__dict__["_calls"] == []
        assert frame.evaluate("window.__clicked === true") is False
        assert clicks == []

    def test_an_unreadable_total_cannot_submit_through_the_frame(
        self, load, page
    ) -> None:
        """A modal whose summary never arrived is refused, not guessed at.

        The button is there, the authorisation is complete and valid, and the
        submission still stops because the total could not be confirmed
        immediately before the click.
        """
        reader = self._load_turbo(load, frame={"summary": False})
        frame = turbo_frame(page)
        clicks: list[str] = []
        watch_turbo_button(page, frame, clicks)

        authorization = self._authorization()
        with pytest.raises(AppError) as excinfo:
            CHECKOUT.submit(reader, authorization)

        assert excinfo.value.code is ErrorCode.CHECKOUT_TOTAL_CHANGED
        assert "could not be re-checked" in excinfo.value.detail
        assert authorization.__dict__["_calls"] == []
        assert frame.evaluate("window.__clicked === true") is False
        assert clicks == []

    def test_submission_is_recorded_before_the_click_in_the_frame(
        self, load, page
    ) -> None:
        """A crash mid-click must leave evidence, wherever the button lives.

        As in the classic case, the host page carries the summary so that the
        submission can get as far as the click at all. The listener awaits the
        exposed binding before setting the flag the test waits on, because the
        binding resolves asynchronously and asserting straight after ``submit``
        would race it.
        """
        order: list[str] = []

        def record() -> None:
            order.append("recorded")

        reader = self._load_turbo(load, host_summary=True)
        frame = turbo_frame(page)
        page.expose_function("__noteClick", lambda: order.append("clicked"))
        frame.evaluate(
            """
            document.querySelector('#turbo-checkout-pyo-button')
              .addEventListener('click', async (event) => {
                  event.preventDefault();
                  await window.__noteClick();
                  window.__clickNoted = true;
              });
            """
        )

        authorization = SubmitAuthorization(
            purchase_job_id=1,
            attempt_id=1,
            approved_total=usd("117.94"),
            guard_passed=True,
            test_mode=False,
            record_submission=record,
        )
        CHECKOUT.submit(reader, authorization)
        # The flag is set on the frame's own window, so the wait runs there.
        frame.wait_for_function("window.__clickNoted === true", timeout=10_000)
        assert order == ["recorded", "clicked"]


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
