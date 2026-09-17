"""Every Amazon selector the application uses, in one place.

Amazon's markup changes, and it A/B tests the buybox, the price block and the
whole checkout pipeline, so two accounts can be served different DOM for the
same item. Two consequences shape this module:

1. **Nothing is a single selector.** Each logical element is a
   :class:`SelectorChain` of ordered candidates. The resolver tries them in
   order and reports which one matched, so a layout change shows up in the
   logs as a fallback being used rather than as a mystery failure.

2. **Selector knowledge lives only here.** No other module contains a CSS
   string. When Amazon changes, this is the only file that needs editing.

The chains are ordered most-specific-first. Where a chain ends in something
broad, the broad candidate is marked ``loose``: the resolver will use it, but
the caller is told, and money-adjacent reads treat a loose match as a reason
to stop rather than to proceed.

A deliberate omission: there is nothing here for solving a CAPTCHA, hiding
automation or defeating a bot check. When Amazon asks for a human, the
application stops and asks the human -- see :mod:`app.automation.login_detector`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Candidate:
    """One way of finding an element."""

    #: A CSS selector, or ``None`` when this candidate is role/text based.
    css: str | None = None
    #: An ARIA role, used with :attr:`name` for accessible-name lookups.
    role: str | None = None
    #: Accessible name, matched case-insensitively as a substring.
    name: str | None = None
    #: Visible-text match, for controls Amazon labels but does not id.
    text: str | None = None
    #: True when this candidate could match something other than the intended
    #: element. Callers doing money-adjacent reads must not trust it silently.
    loose: bool = False

    def describe(self) -> str:
        if self.css:
            return self.css
        if self.role and self.name:
            return f"role={self.role} name~={self.name!r}"
        if self.text:
            return f"text~={self.text!r}"
        return "unspecified"


@dataclass(frozen=True)
class SelectorChain:
    """An ordered set of candidates for one logical element."""

    name: str
    candidates: tuple[Candidate, ...]

    def __iter__(self):
        return iter(self.candidates)

    def __len__(self) -> int:
        return len(self.candidates)


def chain(name: str, *candidates: Candidate) -> SelectorChain:
    return SelectorChain(name=name, candidates=candidates)


def css(selector: str, *, loose: bool = False) -> Candidate:
    return Candidate(css=selector, loose=loose)


def role(role_name: str, accessible_name: str, *, loose: bool = False) -> Candidate:
    return Candidate(role=role_name, name=accessible_name, loose=loose)


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

DEFAULT_MARKETPLACE: Final = "www.amazon.com"


def product_url(asin: str, marketplace: str = DEFAULT_MARKETPLACE) -> str:
    return f"https://{marketplace}/dp/{asin}"


def cart_url(marketplace: str = DEFAULT_MARKETPLACE) -> str:
    return f"https://{marketplace}/gp/cart/view.html"


def orders_url(marketplace: str = DEFAULT_MARKETPLACE) -> str:
    return f"https://{marketplace}/gp/css/order-history"


def account_url(marketplace: str = DEFAULT_MARKETPLACE) -> str:
    return f"https://{marketplace}/gp/css/homepage.html"


def home_url(marketplace: str = DEFAULT_MARKETPLACE) -> str:
    return f"https://{marketplace}/"


def order_details_url(
    order_number: str, marketplace: str = DEFAULT_MARKETPLACE
) -> str:
    return f"https://{marketplace}/gp/css/order-details?orderID={order_number}"


#: ASIN shapes accepted from a pasted URL or typed directly. Amazon ASINs are
#: ten characters: either ``B`` followed by nine alphanumerics, or a ten-digit
#: ISBN for books.
ASIN_PATTERN: Final = re.compile(r"^(?:B[0-9A-Z]{9}|[0-9]{9}[0-9X])$")

#: Places an ASIN appears in an Amazon URL, in order of reliability.
ASIN_URL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"/dp/([A-Z0-9]{10})(?:[/?#]|$)", re.IGNORECASE),
    re.compile(r"/gp/product/(?:glance/)?([A-Z0-9]{10})(?:[/?#]|$)", re.IGNORECASE),
    re.compile(r"/gp/aw/d/([A-Z0-9]{10})(?:[/?#]|$)", re.IGNORECASE),
    re.compile(r"/product/([A-Z0-9]{10})(?:[/?#]|$)", re.IGNORECASE),
    re.compile(r"[?&]ASIN=([A-Z0-9]{10})(?:&|$)", re.IGNORECASE),
    re.compile(r"[?&]asin=([A-Z0-9]{10})(?:&|$)", re.IGNORECASE),
    re.compile(r"/([A-Z0-9]{10})(?:/ref=|[?#]|$)"),
)

#: Hostnames recognised as Amazon storefronts.
AMAZON_HOST_PATTERN: Final = re.compile(
    r"(?:^|\.)amazon\.(?:com|co\.uk|ca|de|fr|it|es|nl|se|pl|com\.au|com\.mx|"
    r"com\.br|co\.jp|in|ae|sa|sg|com\.tr|eg|be)$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Page identity
# ---------------------------------------------------------------------------

#: A product page is only accepted when one of these is present. Amazon serves
#: CAPTCHA and error pages with HTTP 200, so the status code proves nothing --
#: page identity must be established from content.
PRODUCT_PAGE_MARKERS: Final = chain(
    "product_page",
    css("#productTitle"),
    css("#title"),
    css("#dp-container"),
)

CART_PAGE_MARKERS: Final = chain(
    "cart_page",
    css("#sc-active-cart"),
    css("[data-name='Active Items']"),
    css("#sc-cart-empty-message"),
    css("#gutterCartViewForm", loose=True),
)

CHECKOUT_PAGE_MARKERS: Final = chain(
    "checkout_page",
    css("#spc-orders"),
    css("#subtotals-marketplace-table"),
    css("#checkout-primary-continue-button-id"),
    css("[data-testid='SPC_selectPlaceOrder']"),
    css("#submitOrderButtonId"),
)

#: URL fragments that identify each checkout generation.
CLASSIC_CHECKOUT_URL_FRAGMENT: Final = "/gp/buy/spc/"
NEW_CHECKOUT_URL_FRAGMENT: Final = "/checkout/"
THANK_YOU_URL_FRAGMENT: Final = "/gp/buy/thankyou/"


# ---------------------------------------------------------------------------
# Product page
# ---------------------------------------------------------------------------

PRODUCT_TITLE: Final = chain(
    "title",
    css("#productTitle"),
    css("#title span"),
    css("h1#title"),
)

PRODUCT_BRAND: Final = chain(
    "brand",
    css("#bylineInfo"),
    css("a#bylineInfo"),
    css("#brand"),
    css("tr.po-brand td.a-span9 span"),
)

PRODUCT_IMAGE: Final = chain(
    "image",
    css("#landingImage"),
    css("#imgBlkFront"),
    css("#main-image"),
    css("#imageBlock img", loose=True),
)

#: The buybox price. ``.a-offscreen`` holds the screen-reader text, which is a
#: single clean value like ``$109.97``; the visible markup splits the number
#: across ``a-price-whole`` and ``a-price-fraction`` and is read only as a last
#: resort (see :func:`app.core.money.parse_split_price`).
PRODUCT_PRICE: Final = chain(
    "price",
    css("#corePrice_feature_div .a-price .a-offscreen"),
    css("#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen"),
    css("#corePrice_desktop .a-price .a-offscreen"),
    css("#apex_desktop .a-price:not(.a-text-price) .a-offscreen"),
    css("#price_inside_buybox"),
    css(".priceToPay .a-offscreen"),
    css("#newBuyBoxPrice"),
    css("#priceblock_ourprice"),
    css("#priceblock_dealprice"),
    css("#buybox .a-price .a-offscreen", loose=True),
)

#: The split visible price, used only when every ``a-offscreen`` candidate has
#: failed. Read as two separate nodes, never concatenated blindly.
PRODUCT_PRICE_WHOLE: Final = chain(
    "price_whole",
    css("#corePrice_feature_div .a-price-whole"),
    css("#corePriceDisplay_desktop_feature_div .a-price-whole"),
    css(".priceToPay .a-price-whole"),
)
PRODUCT_PRICE_FRACTION: Final = chain(
    "price_fraction",
    css("#corePrice_feature_div .a-price-fraction"),
    css("#corePriceDisplay_desktop_feature_div .a-price-fraction"),
    css(".priceToPay .a-price-fraction"),
)

#: The struck-through "was" price. ``.a-text-price`` alone is not it: Amazon
#: gives the *per-unit* price ("$3.33 per pack") the same class, and a live
#: page on 2026-09-16 was read as having a list price of $3.33 against a real
#: price of $19.99. The per-unit markers are excluded explicitly.
PRODUCT_LIST_PRICE: Final = chain(
    "list_price",
    css("#corePrice_feature_div .basisPrice .a-offscreen"),
    css("#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen"),
    css("#apex_desktop .basisPrice .a-offscreen"),
    css(
        "#centerCol .a-price.a-text-price:not(.apex-priceperunit-value)"
        ":not(.pricePerUnit) .a-offscreen"
    ),
    css(".priceBlockStrikePriceString"),
)

PRODUCT_AVAILABILITY: Final = chain(
    "availability",
    css("#availability span"),
    css("#availability"),
    css("#outOfStock"),
    css("#availability_feature_div"),
)

OUT_OF_STOCK_MARKERS: Final = chain(
    "out_of_stock",
    css("#outOfStock"),
    css("#outOfStockBuyBox_feature_div"),
    css("#backInStock"),
)

#: ``#sellerProfileTriggerId`` is the current template's seller link. The
#: tabular buybox rows are the newer layout; their attribute values are
#: localised, which is why the row is matched by attribute name and the label
#: text is compared separately.
#: Who is selling. ``#sellerProfileTriggerId`` is **not** first any more:
#: signed in, on a live page on 2026-09-16, that element is a link whose
#: entire text is "Learn more about the seller". Signed out, the same id
#: held the seller's name -- which is why the value is judged as well as the
#: element chosen (see :data:`NON_SELLER_LABELS`).
PRODUCT_SELLER: Final = chain(
    "seller",
    css("#merchantInfoFeature_feature_div .offer-display-feature-text-message"),
    css("#tabular-buybox [tabular-attribute-name='Sold by'] .tabular-buybox-text"),
    css("#merchant-info a[href*='seller=']"),
    css("#sellerProfileTriggerId"),
    css("#bylineInfo_feature_div .offer-display-feature-text-message"),
    css("#merchant-info", loose=True),
)

#: Text that is a control's label rather than a seller's name. A seller read
#: from one of these is not a seller: under "Amazon only" it blocks, which is
#: safe but tells the user nothing, and under any other policy it would be
#: stored as the expectation a future purchase is compared against.
NON_SELLER_LABELS: Final[tuple[str, ...]] = (
    "learn more about the seller",
    "learn more",
    "see more",
    "sold by",
    "ships from",
    "visit the store",
    "seller information",
    "other sellers on amazon",
)

PRODUCT_SHIPS_FROM: Final = chain(
    "ships_from",
    css("#tabular-buybox [tabular-attribute-name='Ships from'] .tabular-buybox-text"),
    css("#fulfillerInfoFeature_feature_div .offer-display-feature-text-message"),
    css("#shipsFromFeature_feature_div .offer-display-feature-text-message"),
)

PRODUCT_CONDITION: Final = chain(
    "condition",
    css("#condition-text"),
    css("#usedBuySection .a-text-bold"),
    css("#aod-offer-heading"),
    css("#conditionText"),
)

PRODUCT_QUANTITY_SELECT: Final = chain(
    "quantity_select",
    css("select#quantity"),
    css("#quantity"),
    css("#selectQuantity select"),
)

PRODUCT_ASIN_INPUT: Final = chain(
    "asin_input",
    css("input#ASIN"),
    css("#ASIN"),
    css("input[name='ASIN']"),
    css("input[name='ASIN.0']"),
)

CANONICAL_LINK: Final = chain("canonical", css("link[rel='canonical']"))

PRODUCT_DELIVERY_ESTIMATE: Final = chain(
    "delivery_estimate",
    css("#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE"),
    css("#deliveryBlockMessage"),
    css("#delivery-message"),
    css("#ddmDeliveryMessage"),
)

PRIME_BADGE: Final = chain(
    "prime_badge",
    css("#primeBadge_feature_div i.a-icon-prime"),
    css("#delivery-block i.a-icon-prime"),
    css("i.a-icon-prime"),
    css(".prime-badge"),
)

#: The variation picker. Both the legacy twister and the newer inline rows are
#: in production; the parser reads whichever is present.
TWISTER_CONTAINER: Final = chain(
    "twister",
    css("#twister"),
    css("#twister_feature_div"),
    css("#inline-twister-container"),
    css("#variation_configuration"),
)

#: Legacy twister rows expose the selected value in a ``.selection`` span.
TWISTER_LEGACY_ROWS: Final = "#twister .a-row[id^='variation_']"
TWISTER_LEGACY_LABEL: Final = ".a-form-label"
TWISTER_LEGACY_VALUE: Final = ".selection"

#: Inline twister rows carry the dimension in the row id, and today's markup
#: puts both halves in the row's title: a secondary-coloured "Colour:" span
#: and a ``.inline-twister-dim-title-value`` span holding the selection.
#:
#: Checked against a live amazon.com product page on 2026-09-16: that page has
#: no ``.a-form-label`` and no ``.swatch-title-text-display`` at all, so the
#: title spans are the only readable source. ``.a-form-label`` is kept for the
#: older layout, and the value span must never be used as the *label* -- doing
#: that read "White - 6 Pack" as a dimension name, which is then discarded as
#: unrecognised, and the variation silently became no expectation at all.
TWISTER_INLINE_ROWS: Final = "[id^='inline-twister-row-']"
#: Scoped to the row's heading on purpose: a bare ``.a-color-secondary`` also
#: matches the swatch price ("1 option from $26.98") further down the row.
TWISTER_INLINE_LABEL: Final = (
    ".a-form-label, .dimension-text .a-color-secondary, "
    ".inline-twister-dim-title-value-truncate-expanded .a-color-secondary, "
    ".inline-twister-dim-title-label"
)
TWISTER_INLINE_SELECTED: Final = (
    ".inline-twister-dim-title-value, .swatch-title-text-display, "
    ".a-button-selected .swatch-title-text, li.swatchSelect .swatch-title-text-display"
)

#: Dimension names Amazon uses. Used to tidy a scraped label into something a
#: person recognises, and to drop labels that are not variation dimensions.
KNOWN_VARIATION_LABELS: Final[frozenset[str]] = frozenset(
    {
        "colour",
        "color",
        "size",
        "style",
        "pattern",
        "material",
        "capacity",
        "flavour",
        "flavor",
        "scent",
        "configuration",
        "model",
        "edition",
        "length",
        "width",
        "package quantity",
        "item package quantity",
        "count",
        "number of items",
        "team name",
        "platform",
        "connectivity",
        "wattage",
        "voltage",
        "finish",
        "shape",
    }
)


# ---------------------------------------------------------------------------
# Adding to an order
# ---------------------------------------------------------------------------

ADD_TO_CART_BUTTON: Final = chain(
    "add_to_cart",
    css("#add-to-cart-button"),
    css("input#add-to-cart-button"),
    css("#submit\\.add-to-cart"),
    css("input[name='submit.add-to-cart']"),
    css("#qualifiedBuybox #add-to-cart-button"),
)

BUY_NOW_BUTTON: Final = chain(
    "buy_now",
    css("#buy-now-button"),
    css("input#buy-now-button"),
    css("input[name='submit.buy-now']"),
)

#: Subscribe & Save can be the *default* selection on consumables, in which
#: case Add to Cart enrols a recurring subscription. The one-time row must be
#: selected explicitly, and the guard blocks if that cannot be proven.
SUBSCRIBE_AND_SAVE_ROW: Final = chain(
    "subscribe_and_save_row",
    css("#snsAccordionRowMiddle"),
    css("#snsAccordionRowTop"),
    css("#sns-base-accordion-row"),
)

#: The buy box, used to scope wording checks. A phrase found anywhere on a
#: product page means little -- "Subscribe & Save" appears in recommendation
#: strips -- but the same phrase inside the buy box is about this purchase.
BUY_BOX_CONTAINER: Final = chain(
    "buy_box",
    css("#buybox"),
    css("#desktop_buybox"),
    css("#buyBoxAccordion"),
    css("#rightCol", loose=True),
)

#: When Amazon has the item in more than one condition, the buy box becomes an
#: accordion and the **active** row is the offer that would actually be bought.
#: Checked against a live amazon.com page on 2026-09-16, where the buy box held
#: an active "Buy New $19.99" row and a second "Used - Like New $16.00" row.
#: Reading the active row is the only honest answer there: scanning the box for
#: wording finds the used offer and concludes "condition not stated", which
#: blocks a perfectly ordinary new item.
BUYBOX_ACTIVE_OFFER: Final = chain(
    "buybox_active_offer",
    css("#buyBoxAccordion .a-accordion-active"),
    css("#buybox .a-accordion-active"),
    css("#newAccordionRow_0.a-accordion-active"),
)

#: The alternative-condition offers Amazon advertises inside the buy box. Their
#: wording is about an offer the app is not buying, so it is removed from the
#: text before any condition wording is looked for.
ALTERNATIVE_OFFER_BLOCKS: Final = chain(
    "alternative_offer_blocks",
    css("#usedAccordionRow"),
    css("#usedAccordionCaption_feature_div"),
    css("#usedBuySection"),
)

#: Wording that means an offer is not new. ``"new"`` is deliberately absent:
#: "Used - Like New" contains it, so a positive match on new must come from
#: the active row's caption, never from a substring search.
USED_CONDITION_PHRASES: Final[tuple[str, ...]] = (
    "used - ",
    "used -",
    "renewed",
    "refurbished",
    "pre-owned",
    "open box",
)

#: Wording that means Amazon is offering a recurring delivery. The backstop
#: for a renamed Subscribe & Save row: the ids change, the words do not.
SUBSCRIPTION_TEXT: Final[tuple[str, ...]] = (
    "subscribe & save",
    "subscribe and save",
    "subscribe now",
    "deliver every",
    "delivery every",
    "recurring delivery",
    "auto-delivery",
)

ONE_TIME_PURCHASE_ROW: Final = chain(
    "one_time_purchase_row",
    css("#oneTimeBuyBox"),
    css("#newAccordionRow_0"),
    css("#buyBoxAccordion #oneTimeBuyBox"),
)

ONE_TIME_PURCHASE_RADIO: Final = chain(
    "one_time_purchase_radio",
    css("#oneTimeBuyBox input[type='radio']"),
    css("#oneTimeBuyBox .a-accordion-row-a11y"),
    css("#oneTimeBuyBox a.a-accordion-row-a11y"),
)

#: The protection-plan / warranty interstitial. ``#attachSiNoCoverage`` and its
#: locale variants are the decline control. Note that ``#attachSiDoneButton``
#: and a bare ``#attach-sidesheet`` do **not** exist in current Amazon markup
#: despite appearing in older write-ups, so they are not listed.
ADDON_DECLINE_BUTTON: Final = chain(
    "addon_decline",
    css("#attachSiNoCoverage"),
    css("#attachSiNoCoverage input"),
    css("#attachSiNoCoverage-announce"),
    css("#attachSiNoCoverage-ld"),
    css("#attachSiNoCoverage-ld input"),
    css("#attachSiNoCoverage-eu-enhanced"),
    css("#attachSiNoCoverage-eu-enhanced input"),
    css("#siNoCoverage"),
    role("button", "No thanks"),
)

ADDON_SHEET_MARKERS: Final = chain(
    "addon_sheet",
    css("#attach-warranty-pane"),
    css("#attachSiNoCoverage"),
    css("#attach-accessory-pane"),
    css("#protection-plan-title"),
)

ADDON_SHEET_CLOSE: Final = chain(
    "addon_sheet_close",
    css("#attach-close_sideSheet-link"),
    css("#attach-sidesheet-view-cart-button"),
    css("[data-action='a-popover-close']"),
)

ADD_TO_CART_CONFIRMATION: Final = chain(
    "add_to_cart_confirmation",
    css("#sw-atc-confirmation"),
    css("#NATC_SMART_WAGON_CONF_MSG_SUCCESS"),
    css("#atc-toast-overlay"),
    css("#sc-mini-buy-box"),
    css("#huc-v2-order-row-confirm-text"),
)

NAV_CART_COUNT: Final = chain(
    "nav_cart_count",
    css("#nav-cart-count"),
    css("#nav-cart-count-container .nav-cart-count"),
)


# ---------------------------------------------------------------------------
# Cart
# ---------------------------------------------------------------------------

#: Line items must be read from inside the active-items container. The cart
#: page also renders an "Items you may like" strip whose elements carry
#: ``data-asin``, so a page-wide ``[data-asin]`` scrape returns recommendations
#: even when the cart is completely empty.
CART_ACTIVE_CONTAINER: Final = chain(
    "cart_active_container",
    css("#sc-active-cart [data-name='Active Items']"),
    css("[data-name='Active Items']"),
    css("#sc-active-cart"),
)

CART_LINE_ITEMS: Final = ".sc-list-item[data-asin], .sc-list-item"

CART_EMPTY_MARKERS: Final = chain(
    "cart_empty",
    css("#sc-active-cart .sc-your-amazon-cart-is-empty"),
    css("#sc-cart-empty-message"),
    css(".sc-your-amazon-cart-is-empty"),
)

#: Amazon's own empty-cart wording. Checked before any line parsing, because
#: an empty cart is the safest state to start from.
CART_EMPTY_TEXT: Final[tuple[str, ...]] = (
    "your amazon cart is empty",
    "your shopping cart is empty",
    "your cart is empty",
)

CART_SUBTOTAL: Final = chain(
    "cart_subtotal",
    css("#sc-subtotal-amount-activecart .sc-price"),
    css("#sc-subtotal-amount-activecart"),
    css("#sc-subtotal-amount-buybox .sc-price"),
    css("#sc-subtotal-amount-buybox"),
    css("[data-name='Subtotals'] .sc-price", loose=True),
)

CART_PROCEED_TO_CHECKOUT: Final = chain(
    "proceed_to_checkout",
    css("input[name='proceedToRetailCheckout']"),
    css("#sc-buy-box-ptc-button input"),
    css("#sc-buy-box-ptc-button"),
    css("[name='proceedToRetailCheckout']"),
)

#: Per-row controls. The row uuid changes on every render, so these are always
#: resolved relative to a freshly read row element.
CART_ITEM_SAVE_FOR_LATER: Final = (
    "[data-action='save-for-later'] input, "
    "input[name^='submit.save-for-later'], "
    "[data-action='save-for-later']"
)
CART_ITEM_MOVE_TO_CART: Final = (
    "[data-action='move-to-cart'] input, "
    "input[name^='submit.move-to-cart'], "
    "[data-action='move-to-cart']"
)
CART_ITEM_DELETE: Final = (
    "[data-action='delete-active'] input, "
    "input[name^='submit.delete-active'], "
    "[data-action='delete-active']"
)
CART_ITEM_QUANTITY_INPUT: Final = (
    "input[name='quantityBox'], select[name='sc-quantity'], .sc-quantity-textfield"
)
CART_ITEM_TITLE: Final = ".sc-product-title, .sc-item-content-group .a-truncate-full, h4"
CART_ITEM_PRICE: Final = ".sc-product-price, .sc-price, .sc-badge-price-to-pay"
CART_ITEM_SELLER: Final = ".sc-product-sold-by, .a-size-small.sc-product-sold-by"

#: The saved-for-later section lives on the same URL, below the active cart.
SAVED_FOR_LATER_CONTAINER: Final = chain(
    "saved_for_later",
    css("[data-name='Saved Items']"),
    css("#sc-saved-cart"),
    css("#sc-save-for-later"),
)

#: Rows inside the saved-for-later section. Amazon uses the same row class
#: as the active cart, so this is scoped to that container by its caller.
SAVED_FOR_LATER_ITEMS: Final = ".sc-list-item"


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------

CHECKOUT_ORDER_SUMMARY: Final = chain(
    "order_summary",
    css("#subtotals-marketplace-table"),
    css("#subtotals"),
    css("#order-summary"),
    css("[data-testid='order-summary']"),
    css("#spc-orders", loose=True),
)

#: The order total is read by matching the summary row's *label*, because the
#: id sometimes cited for it (``#subtotal``) could not be confirmed in current
#: markup, and positional table XPath breaks whenever a row is added.
ORDER_TOTAL_LABELS: Final[tuple[str, ...]] = (
    "order total",
    "total",
    "grand total",
)
ITEM_SUBTOTAL_LABELS: Final[tuple[str, ...]] = ("items", "subtotal", "item subtotal")
SHIPPING_LABELS: Final[tuple[str, ...]] = (
    "shipping & handling",
    "shipping and handling",
    "shipping",
    "delivery",
)
TAX_LABELS: Final[tuple[str, ...]] = (
    "estimated tax to be collected",
    "estimated tax",
    "tax",
    "vat",
)
PROMOTION_LABELS: Final[tuple[str, ...]] = (
    "promotion applied",
    "promotion",
    "discount",
    "gift card",
)

#: Rows of the order summary, deliberately leaf elements only. Checked on a
#: live Buy Now checkout on 2026-09-16, where the pipeline is "Chewbacca" and
#: the summary is a list -- ``<li class="a-spacing-mini">Items: $19.99</li>``
#: and ``<li class="grand-total-cell">Order total: $21.44</li>`` -- not the
#: table the older checkout used. A container that holds every row must never
#: be matched: it classifies as "items" and then takes the largest price in
#: it, which is the grand total.
#: Headings that sit in the same containers as the values below them. A
#: candidate that yields one of these has matched the section, not the
#: answer -- and since an empty element no longer ends the search, the next
#: candidate up the page is exactly where a heading gets picked up.
NON_ADDRESS_LABELS: Final[tuple[str, ...]] = (
    "shipping address",
    "delivery address",
    "deliver to",
    "delivering to",
    "ship to",
    "address",
    "choose a delivery address",
)

NON_PAYMENT_LABELS: Final[tuple[str, ...]] = (
    "payment method",
    "payment methods",
    "payment",
    "paying with",
    "pay with",
    "payment information",
    "choose a payment method",
)

CHECKOUT_SUMMARY_ROWS: Final = (
    "#subtotals-marketplace-table tr, "
    "#subtotals tr, "
    "#subtotals li, "
    "#subtotals .order-summary-grid, "
    ".grand-total-cell, "
    "#subtotals-marketplace-table .a-row, "
    "#order-summary .a-row, "
    "[data-testid='order-summary'] .a-row"
)

#: The delivery address. ``#deliver-to-address-text`` is the current
#: checkout's own element and holds the address line by itself; the panel ids
#: around it are kept as fallbacks, and the older layout's markup after that.
#: Checked against a live Buy Now checkout on 2026-09-16, where none of the
#: older candidates matched anything at all.
CHECKOUT_ADDRESS: Final = chain(
    "checkout_address",
    css("#deliver-to-address-text"),
    css("#checkout-deliveryAddressPanel .a-color-base"),
    css("#checkout-delivery-address-panel"),
    css("#addressList .a-color-base.a-text-bold"),
    css(".displayAddressDiv"),
    css("#shipToInsertionNode .displayAddressUL"),
    css("[data-testid='shipping-address'] "),
    css("#shipping-summary"),
    css("#addressListDescription", loose=True),
)

CHECKOUT_PAYMENT: Final = chain(
    "checkout_payment",
    css("#selected-payment-methods-list-container"),
    css("#selected-payment-method-_default"),
    css("#checkout-paymentOptionPanel"),
    css("#checkout-payment-option-panel"),
    css("#payment-information .a-color-base"),
    css("#paymentMethodDisplay"),
    css("[data-testid='payment-information']"),
    css("#spc-payment-summary"),
    css("#payment-summary"),
    css("#existing-credit-cards-box", loose=True),
)

#: Quantity controls on a checkout line, tried before the wording. The newer
#: checkout renders a ``select``; the classic one renders plain text.
CHECKOUT_LINE_QUANTITY_CONTROLS: Final[tuple[str, ...]] = (
    "select[name*='quantity']",
    "input[name*='quantity']",
    "select.quantity",
    "input.quantity",
)

#: Wordings Amazon uses for a line quantity, in order of specificity.
CHECKOUT_QUANTITY_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bqty\s*:?\s*(\d+)", re.IGNORECASE),
    re.compile(r"\bquantity\s*:?\s*(\d+)", re.IGNORECASE),
    re.compile(r"^\s*(\d+)\s*[x×]\s", re.IGNORECASE),
    re.compile(r"\b(\d+)\s*[x×]\s*\$", re.IGNORECASE),
)

#: Price and title inside one checkout line, tried in order. Relative to a
#: line element, so they are plain strings rather than a chain.
CHECKOUT_LINE_PRICE: Final[tuple[str, ...]] = (
    ".a-price .a-offscreen",
    ".a-price",
    ".a-color-price",
)
#: The node inside a checkout line that carries the item's ASIN.
#:
#: On the current checkout there usually is not one. A live third-party order
#: on 2026-09-16 had no ``data-asin`` anywhere in the row -- only a line-item
#: id, a quantity-update URL and the seller's link -- and the Amazon-sold
#: order only had one because a Subscribe & Save upsell inside the row
#: happened to carry it. The guard therefore has to be able to identify a
#: line without an item code; see ``_check_asin_in_checkout``.
CHECKOUT_LINE_ASIN_NODE: Final = "[data-asin]"

#: Who is selling one line of the order. The seller's profile link is the
#: reliable handle on the current checkout: its text is the seller's name and
#: its href carries the seller id. The row also says it in words ("Ships from
#: Amazon.com Sold by Aproca Direct"), which the reader falls back to.
CHECKOUT_LINE_SELLER: Final[tuple[str, ...]] = (
    "a[href*='seller=']",
    ".sc-product-sold-by",
    ".a-size-small.sc-product-sold-by",
    "[data-testid='sold-by']",
)

#: "Sold by X", wherever Amazon words it that way.
SOLD_BY_PATTERN: Final = re.compile(
    r"sold\s+by\s+(.{2,60}?)(?:\s*\||\.|$|\s{2,})", re.IGNORECASE
)

CHECKOUT_LINE_TITLE: Final[tuple[str, ...]] = (
    ".lineitem-title-text",
    ".sc-product-title",
    ".a-size-base",
    "h4",
    ".a-link-normal",
)


def cart_line_for(asin: str) -> str:
    """A selector for one cart line, addressed by its ASIN.

    Cart row ids are regenerated on every render, so ``data-asin`` is the
    only stable handle. The ASIN is validated against
    :data:`ASIN_PATTERN` first: interpolating an unvalidated value into an
    attribute selector is how a malformed identifier becomes a broken query.
    """
    candidate = asin.strip().upper()
    if not ASIN_PATTERN.match(candidate):
        raise ValueError(f"Not a valid ASIN for a selector: {asin!r}")
    return f"{CART_LINE_ITEMS}[data-asin='{candidate}']"


#: One line of the order. ``.lineitem-container`` is the current layout's row
#: -- its id is a base64 blob, so the class is the only stable handle. The
#: ``checkout-item-block`` ids are deliberately **not** matched: they belong
#: to the surrounding panel, the title span and even the "Add gift options"
#: link, and matching them turned one order into six "line items".
#:
#: The row carries its ASIN on a descendant rather than on itself, which the
#: reader handles; the Subscribe & Save upsell inside the row carries the
#: same ASIN, so that costs nothing.
CHECKOUT_LINE_ITEMS: Final = (
    "#spc-orders .a-fixed-left-grid, "
    "#huc-v2-order-row-items .a-fixed-left-grid, "
    "[data-testid='checkout-item'], "
    ".lineitem-container"
)

#: Step-advance controls in the newer pipeline carry stable ids but their
#: labels change per step, so id and text are both matched.
CHECKOUT_CONTINUE: Final = chain(
    "checkout_continue",
    css("#checkout-primary-continue-button-id input"),
    css("#checkout-primary-continue-button-id"),
    css("#checkout-secondary-continue-button-id"),
    css("input[name='shipToThisAddress']"),
    role("button", "Use this address"),
    role("button", "Use this payment method"),
    role("button", "Deliver to this address"),
    role("button", "Continue"),
)

#: The final submit control. Two identical buttons exist on the newer
#: pipeline, one at the top and one at the bottom of the summary, which is why
#: the caller must count matches and act on a single specific one rather than
#: clicking whatever matches first.
PLACE_ORDER_BUTTON: Final = chain(
    "place_order",
    css("[data-testid='SPC_selectPlaceOrder'] input"),
    css("[data-testid='SPC_selectPlaceOrder']"),
    css("input[name='placeYourOrder1']"),
    css("#submitOrderButtonId input"),
    css("#submitOrderButtonId"),
    css("#bottomSubmitOrderButtonId input"),
    css("#bottomSubmitOrderButtonId"),
    css("#placeYourOrder"),
    css("input[name='placeYourOrder']"),
)

#: Buy Now can open a modal checkout inside an iframe. Without waiting for the
#: panel container the order silently fails and the page blanks, so the panel
#: is a required precondition rather than an optimisation.
TURBO_CHECKOUT_IFRAME: Final = "#turbo-checkout-iframe"
TURBO_CHECKOUT_PANEL: Final = chain(
    "turbo_panel",
    css("#turbo-checkout-panel-container"),
    css("#turbo-checkout-panel"),
)
TURBO_PLACE_ORDER: Final = chain(
    "turbo_place_order",
    css("#turbo-checkout-pyo-button"),
    css("#turbo-checkout-pyo-button input"),
    css("#turbo-checkout-pyo"),
)

SUBSCRIPTION_AT_CHECKOUT: Final = chain(
    "subscription_at_checkout",
    css("select[name='snsRecurrencePeriodDropDown']"),
    css("#sns-checkout-row"),
    css(".sns-subscription-detail"),
)

#: Extra items Amazon attaches at checkout. Matched by wording because they
#: appear as ordinary line items rather than in a dedicated container.
ADDON_LINE_MARKERS: Final[tuple[str, ...]] = (
    "protection plan",
    "asurion",
    "allstate protection",
    "accident protection",
    "extended warranty",
    "service plan",
    "installation",
    "haul-away",
    "expert installation",
    "setup service",
)


# ---------------------------------------------------------------------------
# Order confirmation
# ---------------------------------------------------------------------------

CONFIRMATION_MARKERS: Final = chain(
    "order_confirmation",
    css("#widget-purchaseConfirmationStatus"),
    css("#confirmation-message"),
    css("[data-testid='order-confirmation']"),
    css(".thank-you-page"),
)

#: Confirmation wording varies, so it is matched loosely rather than as an
#: exact string.
CONFIRMATION_TEXT_PATTERN: Final = re.compile(
    r"order\s+(?:placed|confirmed|complete)|thank\s*you\s*for\s*your\s*order",
    re.IGNORECASE,
)

#: The thank-you URL carries the order number in ``purchaseId``, which is the
#: cheapest reliable way to read it.
PURCHASE_ID_PATTERN: Final = re.compile(
    r"[?&]purchaseId=(\d{3}-\d{7}-\d{7})", re.IGNORECASE
)

ORDER_NUMBER_PATTERN: Final = re.compile(r"\b(\d{3}-\d{7}-\d{7})\b")

#: Order-history cards, used to verify an order independently of the
#: confirmation page's template.
ORDER_CARD: Final = ".js-order-card, .order-card, .a-box-group.order"
ORDER_CARD_NUMBER_PATTERN: Final = re.compile(
    r"ORDER\s*#\s*(\d{3}-\d{7}-\d{7})", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Session and human-verification detection
# ---------------------------------------------------------------------------

NAV_ACCOUNT_GREETING: Final = chain(
    "account_greeting",
    css("#nav-link-accountList-nav-line-1"),
    css("#nav-link-accountList .nav-line-1"),
    css("#nav-link-accountList"),
)

#: Signed-out greeting wording, matched as a substring after normalisation.
#: Detection keys off the *absence* of this phrase rather than the presence of
#: a name, because the name is locale- and account-dependent.
SIGNED_OUT_GREETINGS: Final[tuple[str, ...]] = (
    "sign in",
    "hello, sign in",
    "identifiez-vous",
    "anmelden",
    "iniciar sesion",
    "accedi",
)

SIGN_IN_FORM: Final = chain(
    "sign_in_form",
    css("form[name='signIn']"),
    css("#ap_password"),
    css("#ap_email"),
    css("#ap_email_login"),
    css("#signInSubmit"),
)

MFA_MARKERS: Final = chain(
    "mfa",
    css("#auth-mfa-otpcode"),
    css("#input-box-otp"),
    css("#auth-mfa-form"),
    css("#cvf-submit-otp-button"),
    css("#auth-select-device-form"),
)

CAPTCHA_MARKERS: Final = chain(
    "captcha",
    css("#captchacharacters"),
    css("form[action*='validateCaptcha']"),
    css("#auth-captcha-image"),
    css("#captcha-container"),
    css("input[name='cvf_captcha_input']"),
)

#: Amazon's throttle / generic error page. Served with HTTP 503 but sometimes
#: with 200, so it is detected by content.
SERVICE_ERROR_MARKERS: Final = chain(
    "service_error",
    css("#g"),
    css("img[alt*='Sorry']"),
    css(".a-container img[src*='dogs']"),
)

SERVICE_ERROR_TEXT: Final[tuple[str, ...]] = (
    "sorry! something went wrong",
    "something went wrong on our end",
    "to discuss automated access to amazon data",
    "enter the characters you see below",
    "type the characters you see in this image",
)

#: URL fragments that identify an interruption.
SIGN_IN_URL_FRAGMENTS: Final[tuple[str, ...]] = ("/ap/signin", "/gp/sign-in")
MFA_URL_FRAGMENTS: Final[tuple[str, ...]] = ("/ap/mfa", "/ap/cvf", "/ap/challenge", "/ap/dcq")
CAPTCHA_URL_FRAGMENTS: Final[tuple[str, ...]] = ("/errors/validatecaptcha",)
ERROR_URL_FRAGMENTS: Final[tuple[str, ...]] = ("/errors/500", "/errors/")


def looks_like_amazon_host(host: str | None) -> bool:
    """Whether ``host`` is an Amazon storefront domain."""
    if not host:
        return False
    return bool(AMAZON_HOST_PATTERN.search(host.strip().lower()))


def split_reference(text: str) -> tuple[str | None, str]:
    """Split user input into an Amazon host (if any) and the rest.

    Accepts input with or without a scheme, because people paste both
    ``https://www.amazon.com/dp/X`` and ``amazon.com/dp/X``.
    """
    candidate = text.strip()
    normalised = candidate
    if "://" not in normalised:
        head = normalised.split("/", 1)[0]
        if "." in head and " " not in head:
            normalised = f"https://{normalised}"
    parsed = urlsplit(normalised)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return parsed.netloc.split("@")[-1].split(":")[0], candidate
    return None, candidate


def extract_asin(text: str | None) -> str | None:
    """Pull an ASIN out of an Amazon URL, or accept one typed directly.

    Returns ``None`` rather than guessing, so an unrecognised link produces a
    clear "that is not an Amazon product link" message instead of a check
    against the wrong item.

    A URL on a non-Amazon host is refused even when it happens to contain
    something ASIN-shaped: another retailer's product id is not an Amazon
    item, and checking the wrong item is worse than refusing the input.
    """
    if not text:
        return None
    candidate = text.strip()
    if not candidate:
        return None

    bare = candidate.upper()
    if ASIN_PATTERN.match(bare):
        return bare

    host, candidate = split_reference(candidate)
    if host is not None and not looks_like_amazon_host(host):
        return None

    for pattern in ASIN_URL_PATTERNS:
        match = pattern.search(candidate)
        if match:
            found = match.group(1).upper()
            if ASIN_PATTERN.match(found):
                return found
    return None


def extract_marketplace(text: str | None) -> str | None:
    """The Amazon host from a pasted URL, or ``None`` when absent."""
    if not text:
        return None
    host, _ = split_reference(text)
    if host and looks_like_amazon_host(host):
        return host.lower()
    return None
