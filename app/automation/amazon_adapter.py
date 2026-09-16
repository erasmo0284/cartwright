"""High-level Amazon operations, composed from the lower-level managers.

Each method here is one thing the application wants to do -- inspect a
product, prepare a purchase, verify an order -- expressed as a sequence of
steps with progress messages the user will actually see. The adapter contains
no selectors and makes no purchase decisions: it navigates, reads, and hands
snapshots to the guard.

Every navigation goes through :meth:`AmazonAdapter.navigate` so that the same
rules apply everywhere: classify the page before reading it, treat an
interruption as an interruption rather than a parse failure, and never
believe an HTTP status.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.automation import selectors
from app.automation.browser_worker import BrowserSession
from app.automation.cart_manager import MANAGER as CART
from app.automation.cart_manager import IsolationJournal, IsolationPlan
from app.automation.checkout_manager import MANAGER as CHECKOUT
from app.automation.checkout_manager import SubmitAuthorization
from app.automation.login_detector import DETECTOR, PageClassification, PageKind, SessionState
from app.automation.page_reader import PageReader
from app.automation.product_parser import PARSER
from app.core.errors import AppError, ErrorCode
from app.core.money import Money
from app.purchasing.models import (
    CartState,
    CartStrategy,
    CheckoutSnapshot,
    OrderConfirmation,
    ProductSnapshot,
    PurchaseRules,
)

logger = logging.getLogger("app.automation.adapter")

#: How long to leave the browser open waiting for the user to sign in.
SIGN_IN_TIMEOUT_SECONDS = 15 * 60

#: How long to wait for the user to clear a verification challenge mid-run.
VERIFICATION_TIMEOUT_SECONDS = 10 * 60

#: How often to re-check whether the human has finished.
HUMAN_POLL_SECONDS = 2.0


@dataclass(frozen=True)
class PreparedPurchase:
    """The state of a purchase that has reached the final review."""

    product: ProductSnapshot
    checkout: CheckoutSnapshot
    strategy: CartStrategy
    journal: IsolationJournal
    addons_declined: tuple[str, ...] = ()


class AmazonAdapter:
    """Navigates Amazon and reports what it finds."""

    # ---- navigation ------------------------------------------------------

    def navigate(
        self,
        session: BrowserSession,
        url: str,
        *,
        expect: PageKind | None = None,
        allow_human_handoff: bool = True,
    ) -> tuple[PageReader, PageClassification]:
        """Load ``url`` and classify what came back.

        Raises :class:`AppError` when Amazon served an interruption, after
        first giving the user a chance to clear it when that is allowed. An
        HTTP status is never consulted: Amazon serves CAPTCHA and error pages
        with 200.
        """
        page = session.page
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001 - translated below
            message = str(exc).lower()
            if "timeout" in message:
                raise AppError(
                    ErrorCode.TIMEOUT, context={"url": url}, cause=exc
                ) from exc
            if "net::err" in message:
                raise AppError(
                    ErrorCode.NETWORK_UNAVAILABLE, context={"url": url}, cause=exc
                ) from exc
            raise

        reader = session.reader
        classification = DETECTOR.classify(reader)

        if classification.needs_human and allow_human_handoff:
            classification = self._wait_for_human(session, classification)
            reader = session.reader

        self._raise_for_interruption(classification)

        if expect is not None and classification.kind is not expect:
            if expect is PageKind.PRODUCT and classification.kind is PageKind.NOT_FOUND:
                raise AppError(
                    ErrorCode.PRODUCT_NOT_FOUND, context={"url": url}
                )
            raise AppError(
                ErrorCode.UNEXPECTED_PAGE,
                context={
                    "expected": expect.value,
                    "actual": classification.kind.value,
                    "url": url,
                },
            )
        return reader, classification

    def _raise_for_interruption(self, classification: PageClassification) -> None:
        if not classification.is_interruption:
            return
        code = classification.error_code or ErrorCode.UNEXPECTED_PAGE
        raise AppError(code, context={"page": classification.kind.value})

    def _wait_for_human(
        self, session: BrowserSession, classification: PageClassification
    ) -> PageClassification:
        """Show the browser and wait for the user to clear a challenge.

        This is the handoff the product promises: automation pauses, the
        window comes forward, and the run continues from where it stopped
        once the person is done. Nothing is typed on their behalf and no
        challenge is solved.
        """
        logger.warning(
            "Waiting for the user to complete a verification",
            extra={"page": classification.kind.value},
        )
        session.show_browser()
        session.step(
            "Amazon needs your attention. Complete the check in the browser window."
        )

        waited = 0.0
        current = classification
        while waited < VERIFICATION_TIMEOUT_SECONDS:
            session.sleep(HUMAN_POLL_SECONDS)
            waited += HUMAN_POLL_SECONDS
            current = DETECTOR.classify(session.reader)
            if not current.needs_human:
                logger.info("Verification cleared by the user")
                session.step("Thanks. Continuing where we left off.")
                return current
        return current

    # ---- account ---------------------------------------------------------

    def check_session(self, session: BrowserSession) -> SessionState:
        """Test whether the stored Amazon session still works.

        Loads the account page rather than the home page: the home page
        renders for a signed-out visitor too, so it proves nothing.
        """
        session.step("Checking your Amazon sign-in...")
        page = session.page
        try:
            page.goto(
                selectors.account_url(), wait_until="domcontentloaded"
            )
        except Exception as exc:  # noqa: BLE001
            raise AppError(
                ErrorCode.NETWORK_UNAVAILABLE, context={"step": "check_session"}, cause=exc
            ) from exc

        state = DETECTOR.session_state(session.reader)
        logger.info(
            "Session checked",
            extra={
                "signed_in": state.signed_in,
                "needs_verification": state.needs_verification,
                "page": state.kind.value,
            },
        )
        return state

    def connect_account(self, session: BrowserSession) -> SessionState:
        """Open Amazon and wait for the user to sign in themselves.

        The application never sees a password: it opens a normal browser
        window at Amazon's own sign-in page and waits. The session then
        persists in the app's dedicated browser profile.
        """
        session.step("Opening Amazon so you can sign in...")
        page = session.page
        page.goto(selectors.account_url(), wait_until="domcontentloaded")
        session.show_browser()

        state = DETECTOR.session_state(session.reader)
        if state.usable:
            session.step("Already signed in.")
            return state

        session.step("Sign in to Amazon in the browser window. Take your time.")
        waited = 0.0
        while waited < SIGN_IN_TIMEOUT_SECONDS:
            session.sleep(HUMAN_POLL_SECONDS)
            waited += HUMAN_POLL_SECONDS
            state = DETECTOR.session_state(session.reader)
            if state.usable:
                logger.info("Amazon account connected")
                session.step("Signed in. You can close the browser window.")
                return state
            if state.needs_verification:
                session.step(
                    "Amazon is asking for a security check. Complete it in the "
                    "browser window."
                )

        raise AppError(
            ErrorCode.NOT_CONNECTED,
            context={"reason": "sign_in_timed_out"},
            detail_override=(
                "Sign-in was not completed, so nothing was connected. You can "
                "try again whenever you like."
            ),
        )

    def open_page(self, session: BrowserSession, url: str) -> None:
        """Open a page for the user to look at, without reading anything."""
        session.step("Opening Amazon...")
        session.page.goto(url, wait_until="domcontentloaded")
        session.show_browser()

    # ---- product ---------------------------------------------------------

    def inspect_product(
        self,
        session: BrowserSession,
        *,
        asin: str,
        marketplace: str = selectors.DEFAULT_MARKETPLACE,
    ) -> ProductSnapshot:
        """Load a product page and read it."""
        url = selectors.product_url(asin, marketplace)
        session.step("Opening the product page...")
        reader, _ = self.navigate(session, url, expect=PageKind.PRODUCT)

        session.step("Reading the product details...")
        snapshot = PARSER.parse(reader, expected_asin=asin, marketplace=marketplace)

        if not snapshot.asin:
            raise AppError(
                ErrorCode.UNEXPECTED_PAGE,
                context={"reason": "asin_not_readable", "requested": asin},
            )
        session.step("Done.")
        return snapshot

    def read_cart(self, session: BrowserSession) -> CartState:
        """Load and read the cart."""
        session.step("Checking your Amazon cart...")
        reader, _ = self.navigate(
            session, selectors.cart_url(), expect=PageKind.CART
        )
        return CART.read_cart(reader)

    # ---- purchase preparation -------------------------------------------

    def prepare_purchase(
        self,
        session: BrowserSession,
        *,
        rules: PurchaseRules,
        plan: IsolationPlan,
        marketplace: str = selectors.DEFAULT_MARKETPLACE,
        journal: IsolationJournal | None = None,
        on_journal_change: "object | None" = None,
    ) -> PreparedPurchase:
        """Take the purchase as far as the final review, and stop there.

        Never submits. The caller decides what happens next, and in test mode
        nothing happens at all beyond this point.
        """
        journal = journal or IsolationJournal(strategy=plan.strategy)
        addons: tuple[str, ...] = ()

        session.step("Opening the product page...")
        url = selectors.product_url(rules.expected_asin, marketplace)
        reader, _ = self.navigate(session, url, expect=PageKind.PRODUCT)

        session.step("Re-reading the product before ordering...")
        product = PARSER.parse(
            reader, expected_asin=rules.expected_asin, marketplace=marketplace
        )

        if not rules.allow_subscription and product.subscription_preselected:
            session.step("Selecting a one-time purchase...")
            if not CART.select_one_time_purchase(reader):
                raise AppError(
                    ErrorCode.SUBSCRIPTION_DETECTED,
                    context={"asin": rules.expected_asin},
                )
            product = PARSER.parse(
                reader, expected_asin=rules.expected_asin, marketplace=marketplace
            )

        if plan.strategy is CartStrategy.BUY_NOW:
            session.step("Starting a separate order for this item only...")
            CART.start_buy_now(reader)
            reader = session.reader
            addons = CART.decline_addons(reader)
        else:
            if plan.strategy is CartStrategy.SET_ASIDE_OTHERS:
                session.step("Setting your other cart items aside...")
                cart_reader, _ = self.navigate(
                    session, selectors.cart_url(), expect=PageKind.CART
                )
                CART.set_aside_foreign_items(
                    cart_reader,
                    target_asin=rules.expected_asin,
                    journal=journal,
                )
                if callable(on_journal_change):
                    on_journal_change(journal)
                reader, _ = self.navigate(session, url, expect=PageKind.PRODUCT)

            session.step("Adding the item to a new order...")
            CART.add_to_cart(reader)
            reader = session.reader
            addons = CART.decline_addons(reader)

            session.step("Checking the order contains only your item...")
            cart_reader, _ = self.navigate(
                session, selectors.cart_url(), expect=PageKind.CART
            )
            cart = CART.read_cart(cart_reader)
            foreign = cart.foreign_lines(rules.expected_asin)
            if foreign:
                raise AppError(
                    ErrorCode.UNEXPECTED_CART_ITEMS,
                    context={
                        "unexpected": [line.display_title for line in foreign[:5]],
                    },
                )

            session.step("Going through checkout...")
            CART.proceed_to_checkout(cart_reader)
            reader = session.reader

        session.step("Reading the order details...")
        classification = DETECTOR.classify(reader)
        if classification.needs_human:
            classification = self._wait_for_human(session, classification)
            reader = session.reader
        self._raise_for_interruption(classification)

        CHECKOUT.continue_through_checkout(reader)
        reader = session.reader

        session.step("Re-checking the total, tax and delivery...")
        checkout = CHECKOUT.read_checkout(reader)

        return PreparedPurchase(
            product=product,
            checkout=checkout,
            strategy=plan.strategy,
            journal=journal,
            addons_declined=addons,
        )

    def refresh_checkout(self, session: BrowserSession) -> CheckoutSnapshot:
        """Re-read the checkout that is already on screen."""
        session.step("Re-checking the order total...")
        return CHECKOUT.read_checkout(session.reader)

    # ---- submission ------------------------------------------------------

    def submit_order(
        self, session: BrowserSession, authorization: SubmitAuthorization
    ) -> OrderConfirmation:
        """Place the order and read Amazon's response.

        The submission itself is delegated to the checkout manager, which
        re-validates the authorisation. Reading the outcome is deliberately
        separate: a click is not an order, and an unreadable response must be
        reported as uncertain rather than assumed either way.
        """
        session.step("Placing your order...")
        CHECKOUT.submit(session.reader, authorization)

        session.step("Waiting for Amazon to confirm...")
        confirmation = CHECKOUT.read_confirmation(session.reader)

        if confirmation.verified:
            session.step("Order confirmed.")
            return confirmation

        # The confirmation page could not be read. Check the order history,
        # which renders from a different template, before giving up. This only
        # ever reads; it cannot place another order.
        session.step("Confirmation unclear. Checking your Amazon orders...")
        try:
            orders_reader, _ = self.navigate(
                session,
                selectors.orders_url(),
                expect=None,
                allow_human_handoff=False,
            )
            verified = CHECKOUT.verify_via_orders_page(
                orders_reader, expected_total=authorization.approved_total
            )
        except AppError:
            logger.warning("Could not reach the orders page to verify", exc_info=True)
            return confirmation

        if verified.verified:
            session.step("Found the order in your Amazon orders.")
            return verified

        logger.warning("Order outcome could not be established")
        return confirmation

    def verify_order(
        self, session: BrowserSession, *, expected_total: Money | None = None
    ) -> OrderConfirmation:
        """Look for a recent order, to resolve an uncertain outcome.

        Read-only by construction: it loads the order history and matches on
        the total. It is offered to the user rather than run automatically,
        because deciding what happened to a possible order is their call.
        """
        session.step("Opening your Amazon orders...")
        reader, _ = self.navigate(
            session, selectors.orders_url(), expect=None, allow_human_handoff=True
        )
        session.step("Looking for the order...")
        return CHECKOUT.verify_via_orders_page(reader, expected_total=expected_total)

    # ---- cleanup ---------------------------------------------------------

    def restore_cart(
        self, session: BrowserSession, journal: IsolationJournal
    ) -> int:
        """Put back anything that was set aside. Safe to call more than once."""
        if not journal.has_pending_restore:
            return 0
        session.step("Putting your other cart items back...")
        reader, _ = self.navigate(
            session, selectors.cart_url(), expect=PageKind.CART,
            allow_human_handoff=False,
        )
        return CART.restore_set_aside_items(reader, journal)


ADAPTER = AmazonAdapter()
