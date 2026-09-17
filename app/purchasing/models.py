"""Domain types shared by the automation adapter, the guard and the UI.

Everything here is an immutable value object. The automation layer produces
snapshots, the guard consumes them, and the UI renders them; none of them can
mutate another's data.

The seller and condition matching rules in this module are safety-critical and
deliberately strict: they match against known-good values rather than
searching for substrings, because a marketplace seller called
"Amazon.com Deals" must never satisfy an "Amazon only" rule.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Final, Mapping

from app.core.money import DEFAULT_CURRENCY, Money
from app.core.timeutil import utcnow


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class SellerPolicy(StrEnum):
    """Which sellers the user is willing to buy from."""

    AMAZON_ONLY = "amazon_only"
    AMAZON_OR_MANUFACTURER = "amazon_or_manufacturer"
    APPROVED_LIST = "approved_list"
    ANY = "any"

    @property
    def label(self) -> str:
        return {
            SellerPolicy.AMAZON_ONLY: "Amazon only",
            SellerPolicy.AMAZON_OR_MANUFACTURER: "Amazon or the manufacturer",
            SellerPolicy.APPROVED_LIST: "Only sellers I approve",
            SellerPolicy.ANY: "Any seller",
        }[self]

    @property
    def description(self) -> str:
        return {
            SellerPolicy.AMAZON_ONLY: (
                "Buy only when Amazon itself is the seller. Safest option."
            ),
            SellerPolicy.AMAZON_OR_MANUFACTURER: (
                "Also allow the brand's own storefront."
            ),
            SellerPolicy.APPROVED_LIST: (
                "Buy only from sellers you have listed by name."
            ),
            SellerPolicy.ANY: (
                "Allow any seller. The price and condition rules still apply."
            ),
        }[self]


class ConditionPolicy(StrEnum):
    """Which item conditions the user is willing to accept."""

    NEW_ONLY = "new_only"
    ALLOW_USED = "allow_used"
    ALLOW_REFURBISHED = "allow_refurbished"

    @property
    def label(self) -> str:
        return {
            ConditionPolicy.NEW_ONLY: "New only",
            ConditionPolicy.ALLOW_USED: "New or used",
            ConditionPolicy.ALLOW_REFURBISHED: "New or refurbished",
        }[self]

    def accepts(self, condition: ItemCondition) -> bool:
        """Whether ``condition`` satisfies this policy.

        An unknown condition never satisfies any policy: if the page did not
        say what is being sold, the program does not buy it.
        """
        if condition is ItemCondition.UNKNOWN:
            return False
        if condition is ItemCondition.NEW:
            return True
        if self is ConditionPolicy.NEW_ONLY:
            return False
        if self is ConditionPolicy.ALLOW_USED:
            return condition in {ItemCondition.USED, ItemCondition.OPEN_BOX}
        if self is ConditionPolicy.ALLOW_REFURBISHED:
            return condition in {ItemCondition.REFURBISHED, ItemCondition.RENEWED}
        return False


class ItemCondition(StrEnum):
    """The condition of the offer actually being sold."""

    NEW = "new"
    USED = "used"
    OPEN_BOX = "open_box"
    REFURBISHED = "refurbished"
    RENEWED = "renewed"
    COLLECTIBLE = "collectible"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            ItemCondition.NEW: "New",
            ItemCondition.USED: "Used",
            ItemCondition.OPEN_BOX: "Open box",
            ItemCondition.REFURBISHED: "Refurbished",
            ItemCondition.RENEWED: "Renewed",
            ItemCondition.COLLECTIBLE: "Collectible",
            ItemCondition.UNKNOWN: "Not stated",
        }[self]

    @classmethod
    def parse(cls, text: str | None) -> ItemCondition:
        """Classify Amazon's condition wording.

        Unrecognised wording maps to :attr:`UNKNOWN` rather than to ``NEW``,
        so an unfamiliar phrase blocks a New-only purchase instead of
        satisfying it.
        """
        if not text:
            return cls.UNKNOWN
        normalised = normalise_label(text)
        if not normalised:
            return cls.UNKNOWN
        # Order matters: "certified refurbished" contains neither "new" first
        # nor "used", and "used - like new" must not be read as New.
        if "renewed" in normalised:
            return cls.RENEWED
        if "refurb" in normalised:
            return cls.REFURBISHED
        if "collectible" in normalised:
            return cls.COLLECTIBLE
        if "open box" in normalised or "open-box" in normalised:
            return cls.OPEN_BOX
        if "used" in normalised or "pre-owned" in normalised or "preowned" in normalised:
            return cls.USED
        if normalised == "new" or normalised.startswith("new "):
            return cls.NEW
        if "brand new" in normalised:
            return cls.NEW
        return cls.UNKNOWN


class Availability(StrEnum):
    """Whether the item can be bought right now."""

    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    PREORDER = "preorder"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            Availability.IN_STOCK: "In stock",
            Availability.OUT_OF_STOCK: "Out of stock",
            Availability.PREORDER: "Pre-order",
            Availability.UNKNOWN: "Unclear",
        }[self]

    @property
    def is_buyable(self) -> bool:
        return self is Availability.IN_STOCK


class PurchaseMode(StrEnum):
    """How far the program may go without the user."""

    ASSISTED = "assisted"
    AUTOMATIC = "automatic"

    @property
    def label(self) -> str:
        return {
            PurchaseMode.ASSISTED: "Confirm before purchase",
            PurchaseMode.AUTOMATIC: "Buy automatically",
        }[self]


class CartStrategy(StrEnum):
    """How the item was isolated for checkout. Recorded for the audit trail."""

    BUY_NOW = "buy_now"
    EMPTY_CART = "empty_cart"
    SET_ASIDE_OTHERS = "set_aside_others"
    BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

_WHITESPACE = re.compile(r"\s+")
_TRAILING_PUNCTUATION = re.compile(r"[\s.,;:!–—-]+$")


def normalise_label(text: str | None) -> str:
    """Lower-case, de-accent and collapse whitespace for comparison.

    Used for every string the guard compares, so that ``"Amazon.com "`` and
    ``"amazon.com"`` are the same seller while remaining distinct from
    ``"Amazon.com Deals"``.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    collapsed = _WHITESPACE.sub(" ", stripped).strip().lower()
    return _TRAILING_PUNCTUATION.sub("", collapsed)


#: Seller names that mean "Amazon is the retailer". Matched exactly after
#: normalisation. Amazon Warehouse and Amazon Resale are deliberately absent:
#: they sell used stock, and treating them as "Amazon" would let a New-only
#: rule through on a used item.
AMAZON_RETAIL_SELLERS: Final[frozenset[str]] = frozenset(
    {
        "amazon",
        "amazon com",
        "amazon.com",
        "amazon.com services llc",
        "amazon.com services, llc",
        "amazon.com sales, inc",
        "amazon.com llc",
        "amazon us",
        "amazon.ca",
        "amazon.co.uk",
        "amazon eu s.a r.l",
        "amazon eu sarl",
        "amazon.de",
        "amazon.fr",
        "amazon.it",
        "amazon.es",
        "amazon.com.au",
        "amazon.co.jp",
        "amazon asia-pacific holdings private limited",
        "amazon retail llc",
        "amazon retail",
    }
)


def is_amazon_retail(seller: str | None) -> bool:
    """True only for Amazon's own retail entity.

    Exact match against :data:`AMAZON_RETAIL_SELLERS`. A substring test is
    deliberately not used: ``"Amazon.com Deals"`` and ``"AmazonBasics Store"``
    are third-party sellers and must not qualify.

    Non-ASCII letters are refused outright. :func:`normalise_label` applies
    NFKD, which folds mathematical-bold and fullwidth characters onto plain
    ASCII, so a storefront literally named with lookalike glyphs would
    otherwise normalise to ``amazon.com`` and satisfy an "Amazon only" rule
    exactly. Amazon's own retail names contain no such characters, so
    refusing them costs nothing.
    """
    if seller and any(ord(character) > 127 for character in seller):
        return False
    return normalise_label(seller) in AMAZON_RETAIL_SELLERS


def fingerprint_dimensions(dimensions: Mapping[str, str] | None) -> str:
    """A stable hash of a variation's dimensions.

    Keys and values are normalised and sorted, so ``{"Size": "L",
    "Color": "Black"}`` and ``{"color": "black", "size": "l"}`` share a
    fingerprint while a genuinely different selection does not.
    """
    if not dimensions:
        return "none"
    canonical = sorted(
        (normalise_label(key), normalise_label(value))
        for key, value in dimensions.items()
        if normalise_label(key) and normalise_label(value)
    )
    if not canonical:
        return "none"
    payload = json.dumps(canonical, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VariationSnapshot:
    """The selected variation of a product, e.g. Colour: Black, Size: Large."""

    dimensions: Mapping[str, str] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return fingerprint_dimensions(self.dimensions)

    @property
    def is_empty(self) -> bool:
        return not self.dimensions

    def describe(self) -> str:
        """Human-readable summary: ``Black, Large``. Empty when unvaried."""
        if not self.dimensions:
            return ""
        return ", ".join(str(value) for value in self.dimensions.values())

    def describe_full(self) -> str:
        """``Colour: Black, Size: Large``, for the guard's expected/actual."""
        if not self.dimensions:
            return "No options"
        return ", ".join(f"{key}: {value}" for key, value in self.dimensions.items())

    def to_json(self) -> str:
        return json.dumps(dict(self.dimensions), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str | None) -> VariationSnapshot:
        if not text:
            return cls()
        try:
            loaded = json.loads(text)
        except (TypeError, ValueError):
            return cls()
        if not isinstance(loaded, dict):
            return cls()
        return cls(
            dimensions={str(key): str(value) for key, value in loaded.items()}
        )

    def matches(self, other: VariationSnapshot) -> bool:
        return self.fingerprint == other.fingerprint


@dataclass(frozen=True)
class ProductSnapshot:
    """Everything read from one visit to a product page.

    A snapshot never carries a "best guess". Anything that could not be read
    reliably is ``None`` or ``UNKNOWN``, and the guard treats those as
    failures for the checks that need them.
    """

    asin: str
    marketplace: str = "www.amazon.com"
    title: str | None = None
    brand: str | None = None
    image_url: str | None = None
    url: str | None = None
    price: Money | None = None
    list_price: Money | None = None
    availability: Availability = Availability.UNKNOWN
    availability_text: str | None = None
    seller: str | None = None
    ships_from: str | None = None
    condition: ItemCondition = ItemCondition.UNKNOWN
    variation: VariationSnapshot = field(default_factory=VariationSnapshot)
    max_quantity: int | None = None
    prime_eligible: bool | None = None
    delivery_estimate: str | None = None
    #: True when the page showed a variation picker (colour, size, style…).
    #: Read separately from :attr:`variation` so that "this product has no
    #: variations" stays distinguishable from "it has variations and none of
    #: them could be read" -- the second is what silently produces a rule
    #: with no expectation at all.
    variation_picker_present: bool = False
    #: True when Amazon has pre-selected a recurring Subscribe & Save option.
    subscription_preselected: bool = False
    #: True when a Buy Now control was present, enabling cart isolation.
    buy_now_available: bool = False
    add_to_cart_available: bool = False
    observed_at: datetime = field(default_factory=utcnow)

    @property
    def currency(self) -> str:
        return self.price.currency if self.price else DEFAULT_CURRENCY

    @property
    def in_stock(self) -> bool:
        return self.availability.is_buyable

    @property
    def display_title(self) -> str:
        return self.title or f"Item {self.asin}"

    @property
    def seller_is_amazon(self) -> bool:
        return is_amazon_retail(self.seller)

    @property
    def variation_unreadable(self) -> bool:
        """The page offered variations and none of them could be read.

        This is the state that quietly produces a rule with no variation
        expectation: the guard can only compare what was stored, so an empty
        expectation means "anything goes" for the life of that rule. The ASIN
        check still pins the identity of the item -- each Amazon variation has
        its own ASIN -- but the user should be told, not left to assume the
        colour they were looking at was recorded.
        """
        return self.variation_picker_present and self.variation.is_empty

    def line_total(self, quantity: int) -> Money | None:
        return None if self.price is None else self.price * quantity


@dataclass(frozen=True)
class PurchaseRules:
    """The user's explicit conditions for a purchase.

    Every field is something the user set, or a conservative default. The
    guard reads only this object, so a rule that is not represented here
    cannot influence a purchase decision.
    """

    expected_asin: str
    quantity: int = 1
    currency: str = DEFAULT_CURRENCY
    max_item_price: Money | None = None
    max_order_total: Money | None = None
    seller_policy: SellerPolicy = SellerPolicy.AMAZON_ONLY
    approved_sellers: tuple[str, ...] = ()
    condition_policy: ConditionPolicy = ConditionPolicy.NEW_ONLY
    expected_variation: VariationSnapshot = field(default_factory=VariationSnapshot)
    expected_seller: str | None = None
    expected_ships_from: str | None = None
    expected_address_label: str | None = None
    expected_payment_label: str | None = None
    require_address_match: bool = True
    require_payment_match: bool = True
    allow_addons: bool = False
    allow_subscription: bool = False
    require_prime: bool = False
    brand: str | None = None
    rules_id: int | None = None

    def seller_allowed(self, seller: str | None) -> bool:
        """Whether ``seller`` satisfies :attr:`seller_policy`.

        A missing seller never satisfies any policy other than ``ANY``: if the
        page did not say who is selling, the program does not buy.
        """
        normalised = normalise_label(seller)
        if self.seller_policy is SellerPolicy.ANY:
            return True
        if not normalised:
            return False
        if is_amazon_retail(seller):
            return True
        if self.seller_policy is SellerPolicy.AMAZON_ONLY:
            return False
        if self.seller_policy is SellerPolicy.AMAZON_OR_MANUFACTURER:
            brand = normalise_label(self.brand)
            if not brand:
                return False
            # Brand storefronts read as the brand, or the brand followed by
            # one of a known set of storefront words. A bare prefix match
            # would accept "Klein Tools Discount Warehouse", which is a
            # different seller with different returns and stock.
            if normalised == brand:
                return True
            suffixes = ("store", "official", "official store", "direct", "us", "usa")
            return any(normalised == f"{brand} {suffix}" for suffix in suffixes)
        if self.seller_policy is SellerPolicy.APPROVED_LIST:
            approved = {normalise_label(name) for name in self.approved_sellers}
            approved.discard("")
            return normalised in approved
        return False

    def condition_allowed(self, condition: ItemCondition) -> bool:
        return self.condition_policy.accepts(condition)

    def describe_seller_rule(self) -> str:
        if self.seller_policy is SellerPolicy.APPROVED_LIST and self.approved_sellers:
            return "Amazon or: " + ", ".join(self.approved_sellers)
        return self.seller_policy.label


@dataclass(frozen=True)
class CartLine:
    """One line item in the Amazon cart or the checkout.

    ``quantity`` is ``None`` when no quantity control could be read. That is
    deliberately distinct from ``1``: defaulting an unreadable quantity to
    one would let the guard validate a fabricated value and report PASS,
    which is how someone ends up buying three of something.
    """

    asin: str | None
    title: str | None
    quantity: int | None
    unit_price: Money | None = None
    line_price: Money | None = None
    row_id: str | None = None
    seller: str | None = None

    @property
    def display_title(self) -> str:
        return self.title or (f"Item {self.asin}" if self.asin else "Unnamed item")

    @property
    def units(self) -> int:
        """The quantity for arithmetic, treating unknown as zero.

        Never use this to decide whether a quantity rule is satisfied -- check
        ``quantity is None`` for that.
        """
        return self.quantity or 0


@dataclass(frozen=True)
class CartState:
    """The contents of the active cart, read before any checkout step."""

    lines: tuple[CartLine, ...] = ()
    subtotal: Money | None = None
    #: ``None`` when the saved-for-later section could not be read, which
    #: is not the same answer as "nothing is saved there".
    saved_for_later_count: int | None = 0
    #: True when Amazon showed its explicit "your cart is empty" message.
    reported_empty: bool = False

    @property
    def is_empty(self) -> bool:
        return self.reported_empty or not self.lines

    @property
    def total_units(self) -> int:
        return sum(line.units for line in self.lines)

    def lines_for(self, asin: str) -> tuple[CartLine, ...]:
        wanted = asin.strip().upper()
        return tuple(
            line
            for line in self.lines
            if line.asin and line.asin.strip().upper() == wanted
        )

    def foreign_lines(self, asin: str) -> tuple[CartLine, ...]:
        """Lines that are not the target item -- the cart-isolation hazard."""
        wanted = asin.strip().upper()
        return tuple(
            line
            for line in self.lines
            if not line.asin or line.asin.strip().upper() != wanted
        )


@dataclass(frozen=True)
class CheckoutSnapshot:
    """The final order review, read immediately before submitting.

    This is the authoritative source for the order-total rule: it is the only
    place where shipping, tax and fees are known.
    """

    lines: tuple[CartLine, ...] = ()
    item_subtotal: Money | None = None
    shipping: Money | None = None
    tax: Money | None = None
    promotion: Money | None = None
    order_total: Money | None = None
    address_label: str | None = None
    payment_label: str | None = None
    #: Names of extra items Amazon attached (protection plans, services).
    addons: tuple[str, ...] = ()
    is_subscription: bool = False
    #: Whether the final submit control was found. Never clicked in test mode.
    place_order_control_found: bool = False
    page_url: str | None = None
    observed_at: datetime = field(default_factory=utcnow)

    @property
    def total_units(self) -> int:
        return sum(line.units for line in self.lines)

    def lines_for(self, asin: str) -> tuple[CartLine, ...]:
        wanted = asin.strip().upper()
        return tuple(
            line
            for line in self.lines
            if line.asin and line.asin.strip().upper() == wanted
        )

    def foreign_lines(self, asin: str) -> tuple[CartLine, ...]:
        wanted = asin.strip().upper()
        return tuple(
            line
            for line in self.lines
            if not line.asin or line.asin.strip().upper() != wanted
        )


@dataclass(frozen=True)
class OrderConfirmation:
    """What Amazon said after the order was submitted.

    ``verified`` is True only when Amazon's own confirmation was positively
    identified. A clicked button is never sufficient.
    """

    verified: bool
    order_number: str | None = None
    order_total: Money | None = None
    delivery_estimate: str | None = None
    page_url: str | None = None
    observed_at: datetime = field(default_factory=utcnow)

    #: ``ClassVar`` is essential here: a plain (or ``Final``) annotation would
    #: make the compiled pattern a dataclass *field*.
    ORDER_NUMBER_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"\b(\d{3}-\d{7}-\d{7})\b"
    )

    @classmethod
    def extract_order_number(cls, text: str | None) -> str | None:
        """Pull an Amazon order number out of arbitrary text.

        The format is fixed at three, seven and seven digits, which makes it
        unambiguous even inside a whole page of text.
        """
        if not text:
            return None
        match = cls.ORDER_NUMBER_PATTERN.search(text)
        return match.group(1) if match else None


@dataclass(frozen=True)
class AccountStatus:
    """The state of the stored Amazon browser session."""

    connected: bool
    account_label: str | None = None
    last_verified_at: datetime | None = None
    needs_verification: bool = False

    @property
    def summary(self) -> str:
        if self.needs_verification:
            return "Needs verification"
        return "Connected" if self.connected else "Not connected"


def snapshot_to_metadata(snapshot: ProductSnapshot) -> dict[str, Any]:
    """A loggable, redaction-safe summary of a product observation."""
    return {
        "asin": snapshot.asin,
        "price_cents": snapshot.price.cents if snapshot.price else None,
        "currency": snapshot.currency,
        "availability": snapshot.availability.value,
        "seller": snapshot.seller,
        "ships_from": snapshot.ships_from,
        "condition": snapshot.condition.value,
        "variation": snapshot.variation.describe_full(),
        "variation_unreadable": snapshot.variation_unreadable,
        "buy_now": snapshot.buy_now_available,
        "subscription_preselected": snapshot.subscription_preselected,
    }
