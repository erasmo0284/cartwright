"""Classifying what Amazon actually served.

The governing fact: **HTTP 200 does not mean success.** Amazon returns
CAPTCHA pages, "something went wrong" pages and sign-in redirects with a 200
status. Every page load must therefore be classified from its content and
URL, never from its status code, and every automation step asserts it is on
the page it expects before reading anything.

When the answer is "Amazon wants a human", the program stops and says so. It
does not attempt to solve a challenge, disguise itself or work around a bot
check -- that is a deliberate product boundary, not an unimplemented feature.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from app.automation import selectors
from app.automation.page_reader import PageReader
from app.core.errors import ErrorCode
from app.purchasing.models import normalise_label

logger = logging.getLogger("app.automation.login")


class PageKind(StrEnum):
    """What kind of page is on screen."""

    PRODUCT = "product"
    CART = "cart"
    CHECKOUT = "checkout"
    CONFIRMATION = "confirmation"
    ORDERS = "orders"
    ACCOUNT = "account"
    SIGN_IN = "sign_in"
    MFA = "mfa"
    CAPTCHA = "captcha"
    SERVICE_ERROR = "service_error"
    NOT_FOUND = "not_found"
    ADDON_SHEET = "addon_sheet"
    UNKNOWN = "unknown"

    @property
    def needs_human(self) -> bool:
        """Whether a person must act before automation can continue."""
        return self in {PageKind.SIGN_IN, PageKind.MFA, PageKind.CAPTCHA}

    @property
    def error_code(self) -> ErrorCode | None:
        """The failure this page represents, if it is a failure."""
        return {
            PageKind.SIGN_IN: ErrorCode.LOGIN_EXPIRED,
            PageKind.MFA: ErrorCode.VERIFICATION_REQUIRED,
            PageKind.CAPTCHA: ErrorCode.VERIFICATION_REQUIRED,
            PageKind.SERVICE_ERROR: ErrorCode.RATE_LIMITED,
            PageKind.NOT_FOUND: ErrorCode.PRODUCT_NOT_FOUND,
        }.get(self)


@dataclass(frozen=True)
class PageClassification:
    """What was on the page, and whether it is safe to carry on."""

    kind: PageKind
    url: str
    title: str
    #: ``None`` when the page gives no signal either way.
    signed_in: bool | None = None
    account_name: str | None = None

    @property
    def needs_human(self) -> bool:
        return self.kind.needs_human

    @property
    def error_code(self) -> ErrorCode | None:
        return self.kind.error_code

    @property
    def is_interruption(self) -> bool:
        return self.kind in {
            PageKind.SIGN_IN,
            PageKind.MFA,
            PageKind.CAPTCHA,
            PageKind.SERVICE_ERROR,
        }

    def describe(self) -> str:
        return f"{self.kind.value} (signed_in={self.signed_in})"


@dataclass(frozen=True)
class SessionState:
    """Whether the stored browser session is usable."""

    signed_in: bool
    account_name: str | None = None
    needs_verification: bool = False
    kind: PageKind = PageKind.UNKNOWN

    @property
    def usable(self) -> bool:
        return self.signed_in and not self.needs_verification


def _url_contains(url: str, fragments: tuple[str, ...]) -> bool:
    lowered = url.lower()
    return any(fragment in lowered for fragment in fragments)


class LoginDetector:
    """Classifies pages and reports the state of the Amazon session."""

    def classify(self, reader: PageReader) -> PageClassification:
        """Decide what page is on screen.

        Interruptions are checked before content types, because Amazon can
        serve a CAPTCHA at a product URL: trusting the URL would make the app
        try to read a price out of a challenge page.
        """
        url = reader.url()
        title = reader.title()
        signed_in, account_name = self._read_greeting(reader)

        kind = self._classify_interruption(reader, url, title)
        if kind is None:
            kind = self._classify_content(reader, url)

        if kind in {PageKind.SIGN_IN, PageKind.MFA}:
            signed_in = False

        classification = PageClassification(
            kind=kind,
            url=url,
            title=title,
            signed_in=signed_in,
            account_name=account_name,
        )
        if classification.is_interruption:
            logger.warning(
                "Amazon served an interruption",
                extra={"kind": kind.value, "url": url, "title": title},
            )
        return classification

    # ---- interruption detection -----------------------------------------

    def _classify_interruption(
        self, reader: PageReader, url: str, title: str
    ) -> PageKind | None:
        if _url_contains(url, selectors.CAPTCHA_URL_FRAGMENTS):
            return PageKind.CAPTCHA
        if _url_contains(url, selectors.MFA_URL_FRAGMENTS):
            return PageKind.MFA
        if _url_contains(url, selectors.SIGN_IN_URL_FRAGMENTS):
            return PageKind.SIGN_IN

        if reader.exists(selectors.CAPTCHA_MARKERS):
            return PageKind.CAPTCHA
        if reader.exists(selectors.MFA_MARKERS):
            return PageKind.MFA

        page_text = normalise_label(reader.page_text(limit=8_000)) or ""
        normalised_title = normalise_label(title) or ""

        for phrase in selectors.SERVICE_ERROR_TEXT:
            if phrase in page_text or phrase in normalised_title:
                # The CAPTCHA wording is in this list too, because the
                # challenge page and the throttle page share a template.
                if "characters" in phrase:
                    return PageKind.CAPTCHA
                return PageKind.SERVICE_ERROR

        if reader.exists(selectors.SIGN_IN_FORM):
            return PageKind.SIGN_IN

        if "couldn t find that page" in page_text or "page not found" in normalised_title:
            return PageKind.NOT_FOUND

        return None

    # ---- content classification -----------------------------------------

    def _classify_content(self, reader: PageReader, url: str) -> PageKind:
        lowered = url.lower()

        if selectors.THANK_YOU_URL_FRAGMENT in lowered or reader.exists(
            selectors.CONFIRMATION_MARKERS
        ):
            return PageKind.CONFIRMATION

        if reader.exists(selectors.ADDON_SHEET_MARKERS, visible_only=True):
            return PageKind.ADDON_SHEET

        if (
            selectors.CLASSIC_CHECKOUT_URL_FRAGMENT in lowered
            or selectors.NEW_CHECKOUT_URL_FRAGMENT in lowered
            or reader.exists(selectors.CHECKOUT_PAGE_MARKERS)
        ):
            return PageKind.CHECKOUT

        if "/gp/cart/" in lowered or reader.exists(selectors.CART_PAGE_MARKERS):
            return PageKind.CART

        if "order-history" in lowered or "/your-orders/" in lowered:
            return PageKind.ORDERS

        if reader.exists(selectors.PRODUCT_PAGE_MARKERS):
            return PageKind.PRODUCT

        if "/gp/css/homepage" in lowered:
            return PageKind.ACCOUNT

        return PageKind.UNKNOWN

    # ---- session state ---------------------------------------------------

    def _read_greeting(self, reader: PageReader) -> tuple[bool | None, str | None]:
        """Read the navigation greeting.

        Detection keys off the *absence* of the sign-in phrase rather than the
        presence of a name, because the name is account- and locale-specific
        while the sign-in wording is a short known set.
        """
        reading = reader.text(selectors.NAV_ACCOUNT_GREETING)
        if not reading.found or reading.value is None:
            return None, None
        normalised = normalise_label(reading.value) or ""
        if not normalised:
            return None, None
        for phrase in selectors.SIGNED_OUT_GREETINGS:
            if phrase in normalised:
                return False, None
        name = reading.value.strip()
        for prefix in ("hello,", "hello", "hi,"):
            if name.lower().startswith(prefix):
                name = name[len(prefix):].strip()
                break
        return True, name or None

    def session_state(self, reader: PageReader) -> SessionState:
        """Assess the session from whatever page is loaded.

        Requires two independent signals to agree before reporting a usable
        session: the navigation greeting must not say "sign in", *and* there
        must be no sign-in form on the page. A single signal is not enough,
        because Amazon sometimes renders a cached signed-in navigation bar on
        a page that then demands re-authentication.
        """
        classification = self.classify(reader)

        if classification.kind is PageKind.CAPTCHA or classification.kind is PageKind.MFA:
            return SessionState(
                signed_in=bool(classification.signed_in),
                account_name=classification.account_name,
                needs_verification=True,
                kind=classification.kind,
            )

        if classification.kind is PageKind.SIGN_IN:
            return SessionState(signed_in=False, kind=classification.kind)

        has_sign_in_form = reader.exists(selectors.SIGN_IN_FORM)
        signed_in = bool(classification.signed_in) and not has_sign_in_form

        return SessionState(
            signed_in=signed_in,
            account_name=classification.account_name if signed_in else None,
            needs_verification=False,
            kind=classification.kind,
        )


#: Stateless, so one instance suffices.
DETECTOR = LoginDetector()
