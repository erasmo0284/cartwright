"""The application's error taxonomy.

Every failure the user can encounter has a code here, a plain-English title,
an explanation and a set of concrete next actions. The UI never invents its
own wording for a failure and never shows a bare exception message, so there
is no path to a generic "Something went wrong."

Adding a new failure mode means adding a code and a :class:`ErrorPresentation`
entry; :func:`describe` raises for unknown codes so a missing entry is caught
by the test suite rather than by a user.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Mapping


class ErrorCode(StrEnum):
    """Stable identifiers for every user-visible failure mode."""

    # --- account / session ------------------------------------------------
    LOGIN_EXPIRED = "login_expired"
    SESSION_INVALID = "session_invalid"
    VERIFICATION_REQUIRED = "verification_required"
    NOT_CONNECTED = "not_connected"

    # --- browser / environment -------------------------------------------
    BROWSER_UNAVAILABLE = "browser_unavailable"
    BROWSER_PROFILE_LOCKED = "browser_profile_locked"
    BROWSER_CLOSED_BY_USER = "browser_closed_by_user"
    NETWORK_UNAVAILABLE = "network_unavailable"
    RATE_LIMITED = "rate_limited"

    # --- product inspection ----------------------------------------------
    INVALID_URL = "invalid_url"
    PRODUCT_NOT_FOUND = "product_not_found"
    PRODUCT_UNAVAILABLE = "product_unavailable"
    PRICE_UNAVAILABLE = "price_unavailable"
    UNEXPECTED_PAGE = "unexpected_page"

    # --- rule violations (Purchase Guard) --------------------------------
    PRICE_ABOVE_LIMIT = "price_above_limit"
    TOTAL_ABOVE_LIMIT = "total_above_limit"
    SELLER_NOT_ALLOWED = "seller_not_allowed"
    SELLER_CHANGED = "seller_changed"
    CONDITION_NOT_ALLOWED = "condition_not_allowed"
    VARIATION_CHANGED = "variation_changed"
    ASIN_MISMATCH = "asin_mismatch"
    QUANTITY_UNAVAILABLE = "quantity_unavailable"
    UNEXPECTED_CART_ITEMS = "unexpected_cart_items"
    UNEXPECTED_ADDONS = "unexpected_addons"
    SUBSCRIPTION_DETECTED = "subscription_detected"
    ADDRESS_PROBLEM = "address_problem"
    PAYMENT_METHOD_PROBLEM = "payment_method_problem"

    # --- cart / checkout --------------------------------------------------
    CART_CONFLICT = "cart_conflict"
    ADD_TO_CART_FAILED = "add_to_cart_failed"
    CHECKOUT_CHANGED = "checkout_changed"
    CHECKOUT_TOTAL_CHANGED = "checkout_total_changed"
    PAYMENT_REJECTED = "payment_rejected"

    # --- order outcome ----------------------------------------------------
    ORDER_RESULT_UNCERTAIN = "order_result_uncertain"
    ORDER_NOT_CONFIRMED = "order_not_confirmed"
    DUPLICATE_BLOCKED = "duplicate_blocked"

    # --- app internals ----------------------------------------------------
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    DATABASE_ERROR = "database_error"
    INTERNAL_ERROR = "internal_error"


class Severity(StrEnum):
    """How prominently a failure should be presented."""

    INFO = "info"
    WARNING = "warning"
    BLOCKED = "blocked"
    ERROR = "error"


class ActionHint(StrEnum):
    """A concrete next step the UI can offer as a button.

    The UI maps each hint to a real control; a failure with no hints shows
    only a dismiss button.
    """

    RETRY = "retry"
    CONNECT_AMAZON = "connect_amazon"
    OPEN_BROWSER = "open_browser"
    SHOW_BROWSER_WINDOW = "show_browser_window"
    RECONNECT = "reconnect"
    CLEAR_SESSION = "clear_session"
    EDIT_RULES = "edit_rules"
    CHECK_PRODUCT_AGAIN = "check_product_again"
    OPEN_PRODUCT_PAGE = "open_product_page"
    OPEN_CART = "open_cart"
    OPEN_AMAZON_ORDERS = "open_amazon_orders"
    REVIEW_AND_CONFIRM = "review_and_confirm"
    PAUSE_MONITORING = "pause_monitoring"
    OPEN_LOGS = "open_logs"
    RUN_DIAGNOSTICS = "run_diagnostics"
    REPAIR_BROWSER = "repair_browser"


@dataclass(frozen=True)
class ErrorPresentation:
    """Everything the UI needs to render a failure."""

    code: ErrorCode
    title: str
    detail: str
    severity: Severity
    actions: tuple[ActionHint, ...] = ()
    #: True when simply trying the same operation again is reasonable.
    retryable: bool = False
    #: True when the operation stopped because a human must do something.
    needs_user: bool = False


def _p(
    code: ErrorCode,
    title: str,
    detail: str,
    severity: Severity,
    actions: tuple[ActionHint, ...] = (),
    *,
    retryable: bool = False,
    needs_user: bool = False,
) -> ErrorPresentation:
    return ErrorPresentation(
        code=code,
        title=title,
        detail=detail,
        severity=severity,
        actions=actions,
        retryable=retryable,
        needs_user=needs_user,
    )


#: The complete catalogue. Wording is aimed at a non-technical reader: it says
#: what happened, what it means for their order, and what to do next.
PRESENTATIONS: Final[Mapping[ErrorCode, ErrorPresentation]] = {
    ErrorCode.LOGIN_EXPIRED: _p(
        ErrorCode.LOGIN_EXPIRED,
        "Amazon sign-in has expired",
        "Amazon signed this browser out, so prices and checkout cannot be "
        "read. Sign in again and monitoring will continue where it left off.",
        Severity.WARNING,
        (ActionHint.RECONNECT, ActionHint.OPEN_BROWSER),
        needs_user=True,
    ),
    ErrorCode.SESSION_INVALID: _p(
        ErrorCode.SESSION_INVALID,
        "Saved Amazon session is no longer usable",
        "The stored browser session could not be reused. Clearing it and "
        "signing in once more resolves this.",
        Severity.WARNING,
        (ActionHint.RECONNECT, ActionHint.CLEAR_SESSION),
        needs_user=True,
    ),
    ErrorCode.VERIFICATION_REQUIRED: _p(
        ErrorCode.VERIFICATION_REQUIRED,
        "Amazon needs your attention",
        "Amazon is asking for a security check, such as a one-time code, a "
        "passkey or an image challenge. Complete it in the browser window and "
        "the app will carry on. Nothing was ordered.",
        Severity.WARNING,
        (ActionHint.SHOW_BROWSER_WINDOW,),
        needs_user=True,
    ),
    ErrorCode.NOT_CONNECTED: _p(
        ErrorCode.NOT_CONNECTED,
        "Amazon account is not connected yet",
        "Connect your Amazon account once so the app can read prices and use "
        "your normal checkout. Your password is never seen or stored by this "
        "app: you sign in directly with Amazon in a normal browser window.",
        Severity.INFO,
        (ActionHint.CONNECT_AMAZON,),
        needs_user=True,
    ),
    ErrorCode.BROWSER_UNAVAILABLE: _p(
        ErrorCode.BROWSER_UNAVAILABLE,
        "The browser could not be started",
        "The private browser this app uses did not start. Repairing the "
        "browser usually fixes it; the diagnostics report has the details.",
        Severity.ERROR,
        (ActionHint.REPAIR_BROWSER, ActionHint.RUN_DIAGNOSTICS, ActionHint.OPEN_LOGS),
        retryable=True,
    ),
    ErrorCode.BROWSER_PROFILE_LOCKED: _p(
        ErrorCode.BROWSER_PROFILE_LOCKED,
        "The browser is already in use",
        "Another copy of the browser still has the saved session open. Close "
        "the extra browser window, then try again.",
        Severity.WARNING,
        (ActionHint.RETRY, ActionHint.REPAIR_BROWSER),
        retryable=True,
    ),
    ErrorCode.BROWSER_CLOSED_BY_USER: _p(
        ErrorCode.BROWSER_CLOSED_BY_USER,
        "The browser window was closed",
        "The browser closed before the step finished, so it was stopped. "
        "Nothing was ordered.",
        Severity.INFO,
        (ActionHint.RETRY,),
        retryable=True,
    ),
    ErrorCode.NETWORK_UNAVAILABLE: _p(
        ErrorCode.NETWORK_UNAVAILABLE,
        "No internet connection",
        "Amazon could not be reached. Monitoring will keep trying, waiting "
        "longer between attempts until the connection is back.",
        Severity.WARNING,
        (ActionHint.RETRY,),
        retryable=True,
    ),
    ErrorCode.RATE_LIMITED: _p(
        ErrorCode.RATE_LIMITED,
        "Amazon is asking the app to slow down",
        "Amazon temporarily refused the request because checks came too often. "
        "Checks will automatically resume more slowly.",
        Severity.WARNING,
        (ActionHint.PAUSE_MONITORING,),
        retryable=True,
    ),
    ErrorCode.INVALID_URL: _p(
        ErrorCode.INVALID_URL,
        "That does not look like an Amazon product link",
        "Paste the web address of an Amazon product page, or the product's "
        "10-character item code.",
        Severity.INFO,
    ),
    ErrorCode.PRODUCT_NOT_FOUND: _p(
        ErrorCode.PRODUCT_NOT_FOUND,
        "Product page could not be found",
        "Amazon did not return a product for that link. It may have been "
        "removed, or the link may point somewhere else.",
        Severity.WARNING,
        (ActionHint.OPEN_PRODUCT_PAGE, ActionHint.CHECK_PRODUCT_AGAIN),
    ),
    ErrorCode.PRODUCT_UNAVAILABLE: _p(
        ErrorCode.PRODUCT_UNAVAILABLE,
        "Product is not available to buy",
        "Amazon shows no purchasable offer right now. If you are watching "
        "this item, checks will continue and you will be told when it returns.",
        Severity.INFO,
        (ActionHint.OPEN_PRODUCT_PAGE,),
    ),
    ErrorCode.PRICE_UNAVAILABLE: _p(
        ErrorCode.PRICE_UNAVAILABLE,
        "No price was shown",
        "Amazon did not display a price that could be read reliably, so no "
        "price rule could be checked. Nothing was ordered.",
        Severity.WARNING,
        (ActionHint.OPEN_PRODUCT_PAGE, ActionHint.CHECK_PRODUCT_AGAIN),
        retryable=True,
    ),
    ErrorCode.UNEXPECTED_PAGE: _p(
        ErrorCode.UNEXPECTED_PAGE,
        "Amazon's page looked different than expected",
        "The page did not contain the details the app needs, which usually "
        "means Amazon changed its layout or showed an interruption. Nothing "
        "was ordered. A diagnostic snapshot was saved.",
        Severity.ERROR,
        (ActionHint.CHECK_PRODUCT_AGAIN, ActionHint.RUN_DIAGNOSTICS),
        retryable=True,
    ),
    ErrorCode.PRICE_ABOVE_LIMIT: _p(
        ErrorCode.PRICE_ABOVE_LIMIT,
        "Price is above your limit",
        "The item costs more than the maximum item price you set, so the "
        "order was not placed.",
        Severity.BLOCKED,
        (ActionHint.EDIT_RULES, ActionHint.OPEN_PRODUCT_PAGE),
    ),
    ErrorCode.TOTAL_ABOVE_LIMIT: _p(
        ErrorCode.TOTAL_ABOVE_LIMIT,
        "Order total is above your limit",
        "With shipping, tax and any fees, the order would cost more than the "
        "maximum order total you set. The order was not placed.",
        Severity.BLOCKED,
        (ActionHint.EDIT_RULES,),
    ),
    ErrorCode.SELLER_NOT_ALLOWED: _p(
        ErrorCode.SELLER_NOT_ALLOWED,
        "Seller is not one you allowed",
        "The offer comes from a seller outside your seller rule, so the order "
        "was not placed.",
        Severity.BLOCKED,
        (ActionHint.EDIT_RULES, ActionHint.OPEN_PRODUCT_PAGE),
    ),
    ErrorCode.SELLER_CHANGED: _p(
        ErrorCode.SELLER_CHANGED,
        "Seller changed",
        "The seller on this offer is not the one recorded when you set this "
        "up. Because a different seller can mean a different product, price "
        "or return policy, the order was not placed.",
        Severity.BLOCKED,
        (ActionHint.OPEN_PRODUCT_PAGE, ActionHint.EDIT_RULES),
    ),
    ErrorCode.CONDITION_NOT_ALLOWED: _p(
        ErrorCode.CONDITION_NOT_ALLOWED,
        "Item condition is not one you allowed",
        "The available offer is not in a condition your rules permit, so the "
        "order was not placed.",
        Severity.BLOCKED,
        (ActionHint.EDIT_RULES, ActionHint.OPEN_PRODUCT_PAGE),
    ),
    ErrorCode.VARIATION_CHANGED: _p(
        ErrorCode.VARIATION_CHANGED,
        "A different version of the product was shown",
        "The size, colour or style on the page is not the one you chose. The "
        "app never substitutes a different version, so the order was not "
        "placed.",
        Severity.BLOCKED,
        (ActionHint.OPEN_PRODUCT_PAGE, ActionHint.CHECK_PRODUCT_AGAIN),
    ),
    ErrorCode.ASIN_MISMATCH: _p(
        ErrorCode.ASIN_MISMATCH,
        "The page showed a different product",
        "The item code on the page does not match the product you chose. The "
        "order was not placed.",
        Severity.BLOCKED,
        (ActionHint.OPEN_PRODUCT_PAGE, ActionHint.CHECK_PRODUCT_AGAIN),
    ),
    ErrorCode.QUANTITY_UNAVAILABLE: _p(
        ErrorCode.QUANTITY_UNAVAILABLE,
        "Requested quantity is not available",
        "Amazon will not sell the number of units you asked for, so the order "
        "was not placed.",
        Severity.BLOCKED,
        (ActionHint.EDIT_RULES, ActionHint.OPEN_PRODUCT_PAGE),
    ),
    ErrorCode.UNEXPECTED_CART_ITEMS: _p(
        ErrorCode.UNEXPECTED_CART_ITEMS,
        "Other items were in the checkout",
        "The checkout contained items other than the one you chose. To make "
        "sure nothing is bought by accident, the order was not placed.",
        Severity.BLOCKED,
        (ActionHint.OPEN_CART,),
    ),
    ErrorCode.UNEXPECTED_ADDONS: _p(
        ErrorCode.UNEXPECTED_ADDONS,
        "An extra add-on was added to the order",
        "Amazon added something extra, such as a protection plan or "
        "installation service. The order was not placed.",
        Severity.BLOCKED,
        (ActionHint.OPEN_CART,),
    ),
    ErrorCode.SUBSCRIPTION_DETECTED: _p(
        ErrorCode.SUBSCRIPTION_DETECTED,
        "This would have started a subscription",
        "The checkout was set up as a recurring Subscribe & Save delivery "
        "rather than a single purchase. The order was not placed.",
        Severity.BLOCKED,
        (ActionHint.OPEN_PRODUCT_PAGE,),
    ),
    ErrorCode.ADDRESS_PROBLEM: _p(
        ErrorCode.ADDRESS_PROBLEM,
        "Delivery address needs attention",
        "The delivery address at checkout is missing, or is not the one you "
        "expected. The order was not placed.",
        Severity.BLOCKED,
        (ActionHint.OPEN_CART, ActionHint.EDIT_RULES),
        needs_user=True,
    ),
    ErrorCode.PAYMENT_METHOD_PROBLEM: _p(
        ErrorCode.PAYMENT_METHOD_PROBLEM,
        "Payment method needs attention",
        "The payment method at checkout is missing, or is not the one you "
        "expected. The order was not placed. Choose your payment method in "
        "Amazon as usual; this app never stores card details.",
        Severity.BLOCKED,
        (ActionHint.OPEN_CART, ActionHint.EDIT_RULES),
        needs_user=True,
    ),
    ErrorCode.CART_CONFLICT: _p(
        ErrorCode.CART_CONFLICT,
        "Your Amazon cart is in the way",
        "Your cart already holds other items, and they could not be set "
        "aside safely. Nothing was ordered and your cart was left as it was.",
        Severity.WARNING,
        (ActionHint.OPEN_CART, ActionHint.RETRY),
        needs_user=True,
    ),
    ErrorCode.ADD_TO_CART_FAILED: _p(
        ErrorCode.ADD_TO_CART_FAILED,
        "The item could not be added",
        "Amazon did not accept the item into the checkout. It may have just "
        "sold out. Nothing was ordered.",
        Severity.WARNING,
        (ActionHint.CHECK_PRODUCT_AGAIN, ActionHint.OPEN_PRODUCT_PAGE),
        retryable=True,
    ),
    ErrorCode.CHECKOUT_CHANGED: _p(
        ErrorCode.CHECKOUT_CHANGED,
        "Amazon's checkout looked different than expected",
        "The checkout page did not contain the details needed to verify the "
        "order, so it was stopped before anything was placed.",
        Severity.ERROR,
        (ActionHint.RUN_DIAGNOSTICS, ActionHint.OPEN_CART),
        retryable=True,
    ),
    ErrorCode.CHECKOUT_TOTAL_CHANGED: _p(
        ErrorCode.CHECKOUT_TOTAL_CHANGED,
        "The total changed at the last moment",
        "The order total was different when it was re-checked immediately "
        "before ordering, so the order was not placed.",
        Severity.BLOCKED,
        (ActionHint.REVIEW_AND_CONFIRM, ActionHint.EDIT_RULES),
    ),
    ErrorCode.PAYMENT_REJECTED: _p(
        ErrorCode.PAYMENT_REJECTED,
        "Amazon declined the payment",
        "Amazon did not accept the payment method for this order. No order "
        "was created. Check the payment method on your Amazon account.",
        Severity.ERROR,
        (ActionHint.OPEN_AMAZON_ORDERS, ActionHint.OPEN_BROWSER),
        needs_user=True,
    ),
    ErrorCode.ORDER_RESULT_UNCERTAIN: _p(
        ErrorCode.ORDER_RESULT_UNCERTAIN,
        "The result of the order is not certain",
        "The order was submitted but Amazon's confirmation could not be read, "
        "so the app cannot tell whether it went through. It will NOT try "
        "again. Check your Amazon orders, then tell the app what you found.",
        Severity.ERROR,
        (ActionHint.OPEN_AMAZON_ORDERS,),
        needs_user=True,
    ),
    ErrorCode.ORDER_NOT_CONFIRMED: _p(
        ErrorCode.ORDER_NOT_CONFIRMED,
        "Amazon did not confirm the order",
        "The order was submitted but Amazon showed a problem instead of a "
        "confirmation, so it was not completed.",
        Severity.ERROR,
        (ActionHint.OPEN_AMAZON_ORDERS, ActionHint.OPEN_CART),
        needs_user=True,
    ),
    ErrorCode.DUPLICATE_BLOCKED: _p(
        ErrorCode.DUPLICATE_BLOCKED,
        "Already ordered",
        "This purchase has already been completed, so it was not ordered a "
        "second time.",
        Severity.INFO,
        (ActionHint.OPEN_AMAZON_ORDERS,),
    ),
    ErrorCode.CANCELLED: _p(
        ErrorCode.CANCELLED,
        "Stopped",
        "The step was stopped before it finished. Nothing was ordered.",
        Severity.INFO,
        (ActionHint.RETRY,),
        retryable=True,
    ),
    ErrorCode.TIMEOUT: _p(
        ErrorCode.TIMEOUT,
        "Amazon took too long to respond",
        "The page did not finish loading in time, so the step was stopped. "
        "Nothing was ordered.",
        Severity.WARNING,
        (ActionHint.RETRY,),
        retryable=True,
    ),
    ErrorCode.DATABASE_ERROR: _p(
        ErrorCode.DATABASE_ERROR,
        "The app could not save its data",
        "Saving to the app's local database failed. Your watch list and "
        "history may be out of date until this is resolved.",
        Severity.ERROR,
        (ActionHint.RUN_DIAGNOSTICS, ActionHint.OPEN_LOGS),
        retryable=True,
    ),
    ErrorCode.INTERNAL_ERROR: _p(
        ErrorCode.INTERNAL_ERROR,
        "The app ran into an unexpected problem",
        "Something failed inside the app rather than on Amazon. Nothing was "
        "ordered. The details were written to the log.",
        Severity.ERROR,
        (ActionHint.OPEN_LOGS, ActionHint.RUN_DIAGNOSTICS),
    ),
}


def describe(code: ErrorCode) -> ErrorPresentation:
    """The presentation for ``code``.

    Raises :class:`KeyError` for an unregistered code so that a missing
    catalogue entry fails loudly in tests instead of silently degrading to a
    generic message in front of a user.
    """
    try:
        return PRESENTATIONS[code]
    except KeyError as exc:  # pragma: no cover - guarded by test_errors.py
        raise KeyError(f"No user-facing text registered for {code!r}") from exc


class AppError(Exception):
    """An application error carrying a taxonomy code and safe context.

    ``context`` is for values that help the user understand the failure and
    that are safe to display and log: expected vs actual seller, a price, a
    step name. It must never hold credentials, cookies or card details.
    """

    def __init__(
        self,
        code: ErrorCode,
        *,
        context: Mapping[str, Any] | None = None,
        detail_override: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.code = code
        self.context: dict[str, Any] = dict(context or {})
        self.detail_override = detail_override
        presentation = describe(code)
        super().__init__(f"{code.value}: {presentation.title}")
        if cause is not None:
            self.__cause__ = cause

    @property
    def presentation(self) -> ErrorPresentation:
        return describe(self.code)

    @property
    def title(self) -> str:
        return self.presentation.title

    @property
    def detail(self) -> str:
        return self.detail_override or self.presentation.detail

    @property
    def severity(self) -> Severity:
        return self.presentation.severity

    @property
    def needs_user(self) -> bool:
        return self.presentation.needs_user

    @property
    def retryable(self) -> bool:
        return self.presentation.retryable

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form for logs, the activity feed and the database."""
        return {
            "code": self.code.value,
            "title": self.title,
            "detail": self.detail,
            "severity": self.severity.value,
            "context": self.context,
        }

    def __str__(self) -> str:
        if self.context:
            pairs = ", ".join(f"{key}={value!r}" for key, value in self.context.items())
            return f"{self.code.value}: {self.title} ({pairs})"
        return f"{self.code.value}: {self.title}"


class OperationCancelled(Exception):
    """Raised inside a worker when the user cancels a running operation.

    Deliberately not an :class:`AppError`: cancellation is a normal outcome,
    so it is never reported as a failure and never triggers retry or backoff.
    """

    def __init__(self, step: str = "") -> None:
        self.step = step
        super().__init__(f"Cancelled during {step}" if step else "Cancelled")
