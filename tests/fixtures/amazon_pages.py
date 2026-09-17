"""Amazon-like HTML fixtures.

These reproduce the *structure* the selectors target -- ids, class names and
nesting -- rather than Amazon's visual design, and they are written from
public knowledge of those identifiers. They exist so the automation layer can
be tested for every scenario that matters (seller change, variation change,
a polluted cart, an add-on at checkout, an over-limit total) without ever
loading the real site and without any possibility of placing an order.

Each builder takes keyword arguments so a test can vary one thing at a time.
The markup deliberately includes the awkward details that break naive
scrapers:

* the price appears twice, in a visible span and in an ``.a-offscreen``
  screen-reader span;
* the visible price is split across ``a-price-whole`` (with a trailing
  separator) and ``a-price-fraction``;
* a struck-through list price sits next to the real one;
* the cart page carries a recommendations strip whose elements also have
  ``data-asin``;
* the checkout summary is a table whose rows are identified by label text,
  not by position.
"""

from __future__ import annotations

from textwrap import dedent

DEFAULT_ASIN = "B07XYZ1234"
DEFAULT_TITLE = "Klein Tools CL800 Digital Clamp Meter"


def _shell(body: str, *, title: str = "Amazon.com") -> str:
    return dedent(
        f"""\
        <!DOCTYPE html>
        <html lang="en"><head><meta charset="utf-8"><title>{title}</title>
        <style>.a-offscreen{{position:absolute;left:-10000px;width:1px;height:1px;
        overflow:hidden}}</style></head>
        <body>{body}</body></html>
        """
    )


def _nav(signed_in: bool = True, account_name: str = "John") -> str:
    greeting = f"Hello, {account_name}" if signed_in else "Hello, sign in"
    return f"""
    <div id="nav-belt">
      <a id="nav-link-accountList" href="/gp/css/homepage.html">
        <span id="nav-link-accountList-nav-line-1" class="nav-line-1">{greeting}</span>
        <span class="nav-line-2">Account &amp; Lists</span>
      </a>
      <a id="nav-cart" href="/gp/cart/view.html">
        <span id="nav-cart-count" class="nav-cart-count">0</span>
      </a>
    </div>
    """


def _price_block(
    price: str | None,
    *,
    list_price: str | None = None,
    include_offscreen: bool = True,
    unit_price: str | None = None,
) -> str:
    """The buybox price block, with Amazon's duplicated/split markup."""
    if price is None:
        return '<div id="corePrice_feature_div"></div>'

    whole, _, fraction = price.partition(".")
    offscreen = (
        f'<span class="a-offscreen">${price}</span>' if include_offscreen else ""
    )
    basis = (
        f"""
        <span class="basisPrice">
          <span class="a-price a-text-price" data-a-strike="true">
            <span class="a-offscreen">${list_price}</span>
            <span aria-hidden="true">${list_price}</span>
          </span>
        </span>
        """
        if list_price
        else ""
    )
    unit = (
        f'<span class="a-size-small a-color-secondary">(${unit_price}/count)</span>'
        if unit_price
        else ""
    )
    return f"""
    <div id="corePrice_feature_div">
      <div class="a-section a-spacing-none aok-align-center">
        <span class="a-price priceToPay" data-a-color="base">
          {offscreen}
          <span aria-hidden="true">
            <span class="a-price-symbol">$</span>
            <span class="a-price-whole">{whole}<span class="a-price-decimal">.</span></span>
            <span class="a-price-fraction">{fraction or '00'}</span>
          </span>
        </span>
        {basis}
        {unit}
      </div>
    </div>
    """


def _buybox_seller(
    seller: str | None,
    ships_from: str | None,
    *,
    seller_link_is_a_label: bool = False,
) -> str:
    """The "Sold by" / "Ships from" block.

    ``seller_link_is_a_label`` reproduces what a signed-in page served on
    2026-09-16: ``#sellerProfileTriggerId`` exists, but its text is "Learn
    more about the seller" and the name lives in the offer-display feature
    instead. Signed out, the same id held the name -- so both shapes are
    real and the parser has to cope with either.
    """
    if seller is None and ships_from is None:
        return ""
    if seller and seller_link_is_a_label:
        return f"""
        <div id="tabular-buybox">
          <div id="merchantInfoFeature_feature_div">
            <span id="merchant-trust-info-card"
                  class="a-size-small offer-display-feature-text-message">{seller}</span>
          </div>
          {f'''<div id="fulfillerInfoFeature_feature_div">
            <span class="a-size-small offer-display-feature-text-message">{ships_from}</span>
          </div>''' if ships_from else ""}
          <a id="sellerProfileTriggerId" href="/gp/help/seller/at-a-glance.html">
            Learn more about the seller</a>
        </div>
        """
    rows = []
    if ships_from:
        rows.append(
            f"""
            <div class="tabular-buybox-container" tabular-attribute-name="Ships from">
              <span class="tabular-buybox-text">{ships_from}</span>
            </div>
            """
        )
    if seller:
        rows.append(
            f"""
            <div class="tabular-buybox-container" tabular-attribute-name="Sold by">
              <span class="tabular-buybox-text">
                <a id="sellerProfileTriggerId"
                   href="/gp/help/seller/at-a-glance.html">{seller}</a>
              </span>
            </div>
            """
        )
    return f'<div id="tabular-buybox">{"".join(rows)}</div>'


def _twister(dimensions: dict[str, str] | None, *, inline: bool = False) -> str:
    if not dimensions:
        return ""
    if inline:
        rows = "".join(
            f"""
            <div id="inline-twister-row-{name.lower()}_name" class="a-section">
              <label class="a-form-label">{name}:</label>
              <span class="inline-twister-dim-title-value">{value}</span>
              <ul class="a-unordered-list">
                <li class="swatchSelect" data-asin="{DEFAULT_ASIN}">
                  <span class="swatch-title-text-display">{value}</span>
                </li>
              </ul>
            </div>
            """
            for name, value in dimensions.items()
        )
        return f'<div id="inline-twister-container">{rows}</div>'

    rows = "".join(
        f"""
        <div id="variation_{name.lower()}_name" class="a-row">
          <label class="a-form-label">{name}:</label>
          <span class="selection">{value}</span>
        </div>
        """
        for name, value in dimensions.items()
    )
    return f'<div id="twister">{rows}</div>'


def _quantity(max_quantity: int) -> str:
    options = "".join(
        f'<option value="{index}">{index}</option>'
        for index in range(1, max_quantity + 1)
    )
    return f'<select id="quantity" name="quantity">{options}</select>'


def _recommendations() -> str:
    """A strip whose items carry ``data-asin`` but are not cart contents.

    Present on real cart pages and the reason a page-wide ``[data-asin]``
    scrape returns items for an empty cart.
    """
    cards = "".join(
        f"""
        <li class="a-carousel-card" data-asin="B0RECOMMEND{index}">
          <span class="p13n-sc-truncate">Recommended accessory {index}</span>
          <span class="a-price"><span class="a-offscreen">$19.99</span></span>
        </li>
        """
        for index in range(1, 5)
    )
    return f"""
    <div id="rhf" class="a-section">
      <h2>Items you may like</h2>
      <ul class="a-carousel">{cards}</ul>
    </div>
    """


# ---------------------------------------------------------------------------
# Product pages
# ---------------------------------------------------------------------------


def product_page(
    *,
    asin: str = DEFAULT_ASIN,
    title: str = DEFAULT_TITLE,
    brand: str = "Klein Tools",
    price: str | None = "109.97",
    list_price: str | None = "129.99",
    unit_price: str | None = None,
    availability: str = "In Stock",
    in_stock: bool = True,
    seller: str | None = "Amazon.com",
    ships_from: str | None = "Amazon.com",
    condition: str | None = None,
    dimensions: dict[str, str] | None = None,
    inline_twister: bool = False,
    max_quantity: int = 30,
    prime: bool = True,
    include_offscreen_price: bool = True,
    subscribe_and_save: bool = False,
    subscribe_preselected: bool = False,
    rename_subscription_ids: bool = False,
    buy_now: bool = True,
    add_to_cart: bool = True,
    signed_in: bool = True,
    seller_link_is_a_label: bool = False,
    delivery: str | None = "FREE delivery Thursday, September 24",
) -> str:
    """A product detail page."""
    if dimensions is None:
        dimensions = {"Color": "Black"}

    availability_block = (
        f'<div id="availability"><span class="a-color-success">{availability}</span></div>'
        if in_stock
        else f'<div id="outOfStock"><div id="availability">'
        f'<span class="a-color-price">{availability}</span></div></div>'
    )

    sns_block = ""
    if subscribe_and_save:
        sns_selected = "a-accordion-active" if subscribe_preselected else ""
        one_time_selected = "" if subscribe_preselected else "a-accordion-active"
        # ``rename_subscription_ids`` models the case the selectors cannot
        # cover: Amazon renames the accordion rows, so every id-based chain
        # misses while the page still offers a recurring delivery.
        sns_id = "snsRowRenamed2027" if rename_subscription_ids else "snsAccordionRowMiddle"
        one_time_id = "oneTimeBoxRenamed2027" if rename_subscription_ids else "oneTimeBuyBox"
        sns_block = f"""
        <div id="buyBoxAccordion" class="a-accordion">
          <div id="{sns_id}" class="a-accordion-row {sns_selected}">
            <a class="a-accordion-row-a11y" href="#">Subscribe &amp; Save</a>
          </div>
          <div id="{one_time_id}" class="a-accordion-row {one_time_selected}">
            <a class="a-accordion-row-a11y" href="#">One-time purchase</a>
            <input type="radio" name="purchase-type" value="one-time"
                   {"" if subscribe_preselected else "checked"}>
          </div>
        </div>
        """

    condition_block = (
        f'<div id="condition-text">{condition}</div>' if condition else ""
    )

    body = f"""
    {_nav(signed_in)}
    <div id="dp-container">
      <div id="centerCol">
        <h1 id="title"><span id="productTitle">{title}</span></h1>
        <a id="bylineInfo" href="/stores/{brand}">Visit the {brand} Store</a>
        {_price_block(price, list_price=list_price,
                      include_offscreen=include_offscreen_price,
                      unit_price=unit_price)}
        {condition_block}
        {_twister(dimensions, inline=inline_twister)}
      </div>
      <div id="imageBlock"><img id="landingImage"
           src="https://m.media-amazon.invalid/images/I/{asin}.jpg" alt="{title}"></div>
      <div id="desktop_buybox">
        <div id="qualifiedBuybox">
          {availability_block}
          {'<div id="primeBadge_feature_div"><i class="a-icon a-icon-prime"></i></div>' if prime else ''}
          {f'<div id="deliveryBlockMessage">{delivery}</div>' if delivery else ''}
          {_quantity(max_quantity)}
          {sns_block}
          {'<input id="add-to-cart-button" name="submit.add-to-cart" type="submit" value="Add to Cart">' if add_to_cart else ''}
          {'<input id="buy-now-button" name="submit.buy-now" type="submit" value="Buy Now">' if buy_now else ''}
          {_buybox_seller(seller, ships_from, seller_link_is_a_label=seller_link_is_a_label)}
        </div>
      </div>
      <input type="hidden" id="ASIN" name="ASIN" value="{asin}">
    </div>
    """
    return _shell(body, title=f"Amazon.com: {title}")


def product_unavailable(**kwargs: object) -> str:
    """A product with no purchasable offer."""
    defaults: dict[str, object] = {
        "price": None,
        "list_price": None,
        "availability": "Currently unavailable.",
        "in_stock": False,
        "seller": None,
        "ships_from": None,
        "buy_now": False,
        "add_to_cart": False,
        "prime": False,
        "delivery": None,
    }
    defaults.update(kwargs)
    return product_page(**defaults)  # type: ignore[arg-type]


def product_not_found() -> str:
    """Amazon's "page not found" body, which still returns HTTP 200."""
    body = f"""
    {_nav(True)}
    <div class="a-container">
      <h1>Sorry! We couldn't find that page.</h1>
      <img src="/dogs.jpg" alt="Dogs of Amazon">
    </div>
    """
    return _shell(body, title="Amazon.com: Page Not Found")


# ---------------------------------------------------------------------------
# Cart pages
# ---------------------------------------------------------------------------


def _cart_line(
    *,
    asin: str,
    title: str,
    quantity: int,
    price: str,
    row_id: str,
    seller: str | None = "Amazon.com",
) -> str:
    return f"""
    <div class="sc-list-item sc-java-remote-feature" data-asin="{asin}"
         data-itemtype="active" id="sc-active-{row_id}">
      <div class="sc-list-item-content">
        <span class="sc-product-title">{title}</span>
        <span class="sc-product-price">${price}</span>
        {f'<span class="sc-product-sold-by">Sold by: {seller}</span>' if seller else ''}
        <input name="quantityBox" value="{quantity}" type="text">
        <span data-action="save-for-later">
          <input name="submit.save-for-later.{row_id}" type="submit" value="Save for later">
        </span>
        <span data-action="delete-active">
          <input name="submit.delete-active.{row_id}" type="submit" value="Delete">
        </span>
      </div>
    </div>
    """


def cart_page(
    *,
    lines: list[dict[str, object]] | None = None,
    subtotal: str | None = None,
    saved_for_later: list[dict[str, object]] | None = None,
    signed_in: bool = True,
) -> str:
    """The cart page. Pass ``lines=[]`` or omit it for an empty cart."""
    lines = lines or []
    saved_for_later = saved_for_later or []

    if not lines:
        # On a genuinely empty cart the subtotal elements are absent from the
        # DOM entirely, rather than present and blank.
        active = """
        <div id="sc-active-cart">
          <div class="sc-your-amazon-cart-is-empty">
            <h1>Your Amazon Cart is empty</h1>
            <p>Shop today's deals</p>
          </div>
        </div>
        """
        subtotal_block = ""
    else:
        items = "".join(
            _cart_line(
                asin=str(line["asin"]),
                title=str(line["title"]),
                quantity=int(line.get("quantity", 1)),  # type: ignore[arg-type]
                price=str(line.get("price", "0.00")),
                row_id=str(line.get("row_id", f"row{index}")),
                seller=line.get("seller", "Amazon.com"),  # type: ignore[arg-type]
            )
            for index, line in enumerate(lines)
        )
        active = f"""
        <div id="sc-active-cart">
          <div data-name="Active Items">{items}</div>
        </div>
        """
        subtotal_block = f"""
        <div data-name="Subtotals">
          <span id="sc-subtotal-label-activecart">
            Subtotal ({len(lines)} items):
          </span>
          <span id="sc-subtotal-amount-activecart">
            <span class="sc-price">${subtotal or '0.00'}</span>
          </span>
        </div>
        <div id="sc-buy-box-ptc-button">
          <input name="proceedToRetailCheckout" type="submit"
                 value="Proceed to checkout">
        </div>
        """

    saved_block = ""
    if saved_for_later:
        saved_items = "".join(
            f"""
            <div class="sc-list-item" data-asin="{line['asin']}"
                 id="sc-saved-{index}">
              <span class="sc-product-title">{line['title']}</span>
              <span data-action="move-to-cart">
                <input name="submit.move-to-cart.saved{index}" type="submit"
                       value="Move to cart">
              </span>
            </div>
            """
            for index, line in enumerate(saved_for_later)
        )
        saved_block = f"""
        <div data-name="Saved Items" id="sc-saved-cart">
          <h2>Saved for later ({len(saved_for_later)} items)</h2>
          {saved_items}
        </div>
        """

    body = f"""
    {_nav(signed_in)}
    <form id="activeCartViewForm">
      <input type="hidden" name="anti-csrftoken-a2z" value="placeholder-token">
      {active}
      {subtotal_block}
      {saved_block}
    </form>
    {_recommendations()}
    """
    return _shell(body, title="Amazon.com Shopping Cart")


def cart_with_target_only(
    *, asin: str = DEFAULT_ASIN, quantity: int = 1, price: str = "109.97"
) -> str:
    return cart_page(
        lines=[
            {
                "asin": asin,
                "title": DEFAULT_TITLE,
                "quantity": quantity,
                "price": price,
                "row_id": "target",
            }
        ],
        subtotal=price,
    )


def cart_with_other_items(*, asin: str = DEFAULT_ASIN) -> str:
    """The cart-isolation hazard: the target plus unrelated pre-existing items."""
    return cart_page(
        lines=[
            {
                "asin": asin,
                "title": DEFAULT_TITLE,
                "quantity": 1,
                "price": "109.97",
                "row_id": "target",
            },
            {
                "asin": "B0DOGFOOD01",
                "title": "Large bag of dog food",
                "quantity": 2,
                "price": "54.00",
                "row_id": "other1",
            },
            {
                "asin": "B0KETTLE001",
                "title": "Electric kettle",
                "quantity": 1,
                "price": "39.99",
                "row_id": "other2",
            },
        ],
        subtotal="257.96",
    )


# ---------------------------------------------------------------------------
# Checkout pages
# ---------------------------------------------------------------------------


def _summary_row(label: str, value: str, *, bold: bool = False) -> str:
    weight = ' class="grand-total-price a-color-price a-text-bold"' if bold else ""
    return f"""
    <tr class="a-spacing-none">
      <td class="a-color-base a-text-left">{label}:</td>
      <td class="a-color-base a-text-right"><span{weight}>{value}</span></td>
    </tr>
    """


def _summary_table(
    *, item_subtotal: str, shipping: str, tax: str, order_total: str
) -> str:
    """The order-summary table, identified by row label rather than position.

    Shared by the classic checkout and by the Buy Now modal, because Amazon
    renders the same table in both -- the only difference being which document
    it lives in, which is exactly what the Turbo tests are about.
    """
    return f"""
    <div id="subtotals">
      <table id="subtotals-marketplace-table">
        {_summary_row("Items", f"${item_subtotal}")}
        {_summary_row("Shipping &amp; handling", f"${shipping}")}
        {_summary_row("Estimated tax to be collected", f"${tax}")}
        {_summary_row("Order total", f"${order_total}", bold=True)}
      </table>
    </div>
    """


def checkout_page(
    *,
    asin: str = DEFAULT_ASIN,
    title: str = DEFAULT_TITLE,
    quantity: int = 1,
    item_price: str = "109.97",
    item_subtotal: str = "109.97",
    shipping: str = "0.00",
    tax: str = "7.97",
    order_total: str = "117.94",
    address: str | None = "John D., Raleigh, NC 27601",
    payment: str | None = "Visa ending in 1234",
    addon: str | None = None,
    subscription: bool = False,
    place_order_button: bool = True,
    duplicate_place_order: bool = True,
    signed_in: bool = True,
) -> str:
    """The classic single-page checkout, with the order summary as a table."""
    addon_line = (
        f"""
        <div class="a-fixed-left-grid lineitem-container" data-asin="B0WARRANTY1">
          <span class="a-size-base">{addon}</span>
          <span class="a-price"><span class="a-offscreen">$24.99</span></span>
          <span class="quantity">Qty: 1</span>
        </div>
        """
        if addon
        else ""
    )

    subscription_block = (
        """
        <div id="sns-checkout-row">
          <select name="snsRecurrencePeriodDropDown">
            <option value="1" selected>Every month</option>
          </select>
        </div>
        """
        if subscription
        else ""
    )

    bottom_button = (
        """
        <span id="bottomSubmitOrderButtonId" data-testid="SPC_selectPlaceOrder">
          <input type="submit" name="placeYourOrder1" value="Place your order"
                 title="Place your order">
        </span>
        """
        if place_order_button and duplicate_place_order
        else ""
    )

    body = f"""
    {_nav(signed_in)}
    <div id="checkout-container">
      <div id="shipping-summary">
        <h2>Shipping address</h2>
        {f'<div class="displayAddressDiv">{address}</div>' if address else
         '<div class="displayAddressDiv"></div>'}
      </div>
      <div id="payment-information">
        <h2>Payment method</h2>
        {f'<div id="paymentMethodDisplay" class="a-color-base">{payment}</div>' if payment else
         '<div id="paymentMethodDisplay" class="a-color-base"></div>'}
      </div>
      <div id="spc-orders">
        <div id="huc-v2-order-row-items">
          <div class="a-fixed-left-grid lineitem-container" data-asin="{asin}">
            <span class="a-size-base sc-product-title">{title}</span>
            <span class="a-price"><span class="a-offscreen">${item_price}</span></span>
            <span class="quantity">Qty: {quantity}</span>
          </div>
          {addon_line}
        </div>
        {subscription_block}
      </div>
      {_summary_table(item_subtotal=item_subtotal, shipping=shipping,
                      tax=tax, order_total=order_total)}
      {'<span id="submitOrderButtonId" data-testid="SPC_selectPlaceOrder">'
       '<input type="submit" name="placeYourOrder1" value="Place your order" '
       'title="Place your order"></span>' if place_order_button else ''}
      {bottom_button}
    </div>
    """
    return _shell(body, title="Amazon.com Checkout")


# ---------------------------------------------------------------------------
# Buy Now (Turbo) checkout
# ---------------------------------------------------------------------------

#: Where the Buy Now modal's document is served from. It is a *separate*
#: document from the page that hosts it, so a test must register this address
#: with the fixture site alongside the host page's -- see the ``also``
#: argument of the ``load`` fixture. Without a route of its own the frame would
#: be an unrouted request, which is the harness telling the truth: the modal is
#: a second page load, not a fragment of the first.
TURBO_IFRAME_URL = "https://www.amazon.com/checkout/turbo-checkout-iframe.html"


#: A line row's id on the current checkout is a base64 blob. Tests must not
#: depend on it, which is the point of including one that looks real.
CURRENT_LINE_ID = (
    "bWlxOi8vZG9jdW1lbnQ6MS4wL09yZGVyaW5nL2FtYXpvbjoxLjAvTGluZUl0ZW06MS4wLzdlYjJlMWNi"
)


def current_checkout_page(
    *,
    asin: str = DEFAULT_ASIN,
    title: str = DEFAULT_TITLE,
    item_price: str = "19.99",
    item_subtotal: str = "19.99",
    shipping: str = "0.00",
    tax: str = "1.45",
    order_total: str = "21.44",
    address: str | None = "1 EXAMPLE RD, EXAMPLETOWN, NC, 27500, United States",
    payment: str | None = "Paying with Visa 1111",
    show_quantity: int | None = None,
    subscribe_and_save_upsell: bool = True,
    place_order_button: bool = True,
) -> str:
    """Amazon's current checkout, as served for a Buy Now order.

    Reproduced from a live page on 2026-09-16, because every earlier
    assumption in this file was about the older layout and none of it
    matched. The details that matter, each of which broke something:

    * the order summary is a ``<ul>`` of ``<li>`` rows -- "Items: $19.99" --
      and the grand total is ``li.grand-total-cell``, not a table row;
    * the delivery address has its own element, ``#deliver-to-address-text``;
    * the payment panel's ``textContent`` is hundreds of characters of inline
      JSON wrapped around the twenty that name the card, so it can only be
      read as *visible* text;
    * the line row is ``div.lineitem-container`` with a base64 id, and its
      ASIN sits on a descendant, not on the row;
    * **no quantity is printed at all** for a single item, which is why the
      guard derives it from the item subtotal;
    * the Subscribe & Save upsell inside the row carries the same ASIN, and
      the surrounding ``checkout-item-block`` ids wrap things that are not
      line items.
    """
    quantity_markup = (
        f'<span class="a-size-mini">Qty: {show_quantity}</span>'
        if show_quantity is not None
        else ""
    )

    upsell = (
        f"""
        <div id="sns-item-v2-0" class="a-section" data-asin="{asin}">
          <span>Subscribe &amp; Save: Save up to 5% on future auto-deliveries</span>
        </div>
        """
        if subscribe_and_save_upsell
        else ""
    )

    # The payment panel's real shape: a script's worth of JSON sitting in the
    # same element as the visible text.
    payment_block = (
        f"""
        <div id="checkout-paymentOptionPanel" class="a-cardui panel-content">
          <div id="selected-payment-methods-list-container" class="a-section">
            <div id="selected-payment-method-_default" class="a-row">
              <span class="a-declarative">{payment}</span>
              <span class="aok-hidden" style="display:none">{{"enrollmentHeader":"Shop with Points at Amazon.com","swpUnavailableMessage":"\u003cdiv id=\"swp-service-unavailable\" class=\"a-box a-alert\"\u003e"}}</span>
            </div>
          </div>
        </div>
        """
        if payment
        else '<div id="checkout-paymentOptionPanel"></div>'
    )

    address_block = (
        f"""
        <div id="checkout-delivery-address-panel" class="checkout-experience-panel">
          <div id="checkout-deliveryAddressPanel" class="a-cardui panel-content">
            <span>Delivering to</span>
            <div id="deliver-to-address-text" class="a-color-base">{address}</div>
            <a id="change-delivery-link" href="#">Change</a>
          </div>
        </div>
        """
        if address
        else '<div id="checkout-deliveryAddressPanel"></div>'
    )

    order_button = (
        """
        <span id="submitOrderButtonId" data-testid="SPC_selectPlaceOrder">
          <input id="placeOrder" type="submit" name="placeYourOrder1"
                 value="Place your order" title="Place your order">
        </span>
        <span id="bottomSubmitOrderButtonId" data-testid="SPC_selectPlaceOrder">
          <input id="placeOrder" type="submit" name="placeYourOrder1"
                 value="Place your order" title="Place your order">
        </span>
        """
        if place_order_button
        else ""
    )

    body = f"""
    {_nav(True)}
    <div id="checkout-experience-container" class="a-row a-ws-row">
      <div id="checkout-experience-left-column" class="a-column">
        {address_block}
        {payment_block}
        <div id="checkout-item-block-panel" class="checkout-experience-panel spp-panel">
          <div id="checkout-item-block-0" class="a-cardui checkout-experience-block">
            <span>Arriving tomorrow</span>
            <div id="{CURRENT_LINE_ID}"
                 class="a-box a-spacing-top-base lineitem-container checkout-card-content">
              <div class="a-fixed-left-grid" data-asin="{asin}">
                <span id="checkout-item-block-item-primary-title-{CURRENT_LINE_ID}"
                      class="lineitem-title-text break-word">{title}</span>
                <span class="a-price apex-price-to-pay-value">
                  <span class="a-offscreen">${item_price}</span>
                </span>
                {quantity_markup}
              </div>
              {upsell}
            </div>
            <span id="checkout-item-block-item-product-trends-{CURRENT_LINE_ID}"
                  class="a-size-mini">20K+ bought in past month</span>
            <a id="checkout-item-block-gift-options-link-{CURRENT_LINE_ID}"
               class="a-link-normal" href="#">Add gift options</a>
          </div>
        </div>
      </div>
      <div id="checkout-experience-right-column" class="a-column">
        <div id="subtotals" class="a-cardui rcx-checkout-custom-card">
          <div class="a-section">
            <span>Place your order</span>
            {order_button}
          </div>
          <hr class="a-divider-normal">
          <div>
            <ul class="a-unordered-list a-nostyle">
              <li class="a-spacing-mini">
                <div class="order-summary-grid">
                  <div class="order-summary-line-term">Items:</div>
                  <div class="order-summary-line-definition">${item_subtotal}</div>
                </div>
              </li>
              <li class="a-spacing-mini">
                <div class="order-summary-grid">
                  <div class="order-summary-line-term">Shipping &amp; handling:</div>
                  <div class="order-summary-line-definition">${shipping}</div>
                </div>
              </li>
              <li class="a-spacing-mini">
                <div class="order-summary-grid">
                  <div class="order-summary-line-term">Estimated tax to be collected:</div>
                  <div class="order-summary-line-definition">${tax}</div>
                </div>
              </li>
              <li class="grand-total-cell">
                <span class="a-list-item a-size-medium a-color-base a-text-bold">
                  Order total: ${order_total}
                </span>
              </li>
            </ul>
          </div>
        </div>
      </div>
    </div>
    """
    return _shell(body, title="Place Your Order - Amazon Checkout")


def turbo_checkout_frame(
    *,
    asin: str = DEFAULT_ASIN,
    title: str = DEFAULT_TITLE,
    quantity: int = 1,
    item_price: str = "109.97",
    item_subtotal: str = "109.97",
    shipping: str = "0.00",
    tax: str = "7.97",
    order_total: str = "117.94",
    address: str | None = "John D., Raleigh, NC 27601",
    payment: str | None = "Visa ending in 1234",
    panel: bool = True,
    place_order_button: bool = True,
    summary: bool = True,
) -> str:
    """The document *inside* ``#turbo-checkout-iframe``.

    This is the whole of a Buy Now purchase: the address, the payment method,
    the line item and the order summary all live in here, and so does the only
    button that places the order. Nothing in this document is reachable from a
    locator built against the page that hosts it.

    ``summary=False`` models a panel that rendered without its order summary,
    which is how a total becomes genuinely unreadable now that the frame is
    read properly -- the case the submit barrier has to refuse.

    ``panel=False`` models the modal that opened but never finished rendering
    -- Amazon serves the frame, the spinner runs, and the panel container never
    arrives. The order button is still rendered in that state on purpose, so a
    test can prove that it is the *panel* that gates the Turbo path and not the
    mere presence of a button.
    """
    if not panel:
        body = """
        <div id="turbo-checkout-spinner" class="a-spinner-wrapper">
          <span class="a-spinner a-spinner-medium"></span>
        </div>
        """
        if place_order_button:
            body += _turbo_place_order_button()
        return _shell(body, title="Amazon.com Checkout")

    body = f"""
    <div id="turbo-checkout-panel-container">
      <div id="turbo-checkout-panel" class="a-section">
        <div id="turbo-checkout-shipping-address-container">
          <h5>Deliver to</h5>
          {f'<div class="displayAddressDiv">{address}</div>' if address else
           '<div class="displayAddressDiv"></div>'}
        </div>
        <div id="payment-information">
          <h5>Pay with</h5>
          {f'<div id="paymentMethodDisplay" class="a-color-base">{payment}</div>'
           if payment else
           '<div id="paymentMethodDisplay" class="a-color-base"></div>'}
        </div>
        <div id="spc-orders">
          <div id="huc-v2-order-row-items">
            <div class="a-fixed-left-grid lineitem-container" data-asin="{asin}">
              <span class="a-size-base sc-product-title">{title}</span>
              <span class="a-price"><span class="a-offscreen">${item_price}</span></span>
              <span class="quantity">Qty: {quantity}</span>
            </div>
          </div>
        </div>
        {_summary_table(item_subtotal=item_subtotal, shipping=shipping,
                        tax=tax, order_total=order_total) if summary else ''}
        {_turbo_place_order_button() if place_order_button else ''}
      </div>
    </div>
    """
    return _shell(body, title="Amazon.com Checkout")


def _turbo_place_order_button() -> str:
    """The modal's submit control.

    The outer span carries the id the selectors target; the inner input is what
    the user actually presses. A click on the span lands on the input and
    bubbles back, which is why a listener on either one sees it.
    """
    return """
    <span id="turbo-checkout-pyo-button" class="a-button a-button-primary">
      <input id="turbo-checkout-place-order-button" type="submit"
             name="placeYourOrder1" value="Place your order"
             title="Place your order">
    </span>
    """


def turbo_checkout_page(
    *,
    asin: str = DEFAULT_ASIN,
    title: str = DEFAULT_TITLE,
    price: str = "109.97",
    iframe: bool = True,
    iframe_src: str = TURBO_IFRAME_URL,
    host_summary: bool = False,
    host_item_subtotal: str = "109.97",
    host_shipping: str = "0.00",
    host_tax: str = "7.97",
    host_order_total: str = "117.94",
    host_place_order_button: bool = False,
    signed_in: bool = True,
) -> str:
    """The page that *hosts* the Buy Now modal: a product page plus an overlay.

    This is what Amazon serves when Buy Now opens a modal checkout instead of
    navigating to ``/gp/buy/spc/``. The product page stays where it was and an
    overlay containing ``#turbo-checkout-iframe`` is drawn on top of it, so the
    host document has no order summary, no address, no payment method and no
    order button -- they are all in the frame.

    ``host_summary`` and ``host_place_order_button`` deliberately contradict
    that. They exist because :class:`~app.automation.checkout_manager.CheckoutManager`
    reads the checkout through a reader bound to *this* document while finding
    the button in the frame, so without a total on the host page a submission
    can never get past its own total check and no test could ever reach the
    click. A test that turns them on is testing the button, not the reader, and
    says so.
    """
    whole, _, fraction = price.partition(".")
    overlay = (
        f"""
        <div id="turbo-checkout-overlay" class="a-popover a-popover-modal">
          <iframe id="turbo-checkout-iframe" name="turbo-checkout-iframe"
                  title="Buy Now checkout" src="{iframe_src}"
                  width="760" height="520" frameborder="0"></iframe>
        </div>
        """
        if iframe
        else ""
    )

    body = f"""
    {_nav(signed_in)}
    <div id="dp-container">
      <div id="centerCol">
        <h1 id="title"><span id="productTitle">{title}</span></h1>
        <div id="corePrice_feature_div">
          <span class="a-price priceToPay">
            <span class="a-offscreen">${price}</span>
            <span aria-hidden="true">
              <span class="a-price-whole">{whole}</span>
              <span class="a-price-fraction">{fraction or '00'}</span>
            </span>
          </span>
        </div>
      </div>
      <div id="desktop_buybox">
        <div id="qualifiedBuybox">
          <div id="availability"><span class="a-color-success">In Stock</span></div>
          <input id="buy-now-button" name="submit.buy-now" type="submit"
                 value="Buy Now">
        </div>
      </div>
      <input type="hidden" id="ASIN" name="ASIN" value="{asin}">
    </div>
    {_summary_table(item_subtotal=host_item_subtotal, shipping=host_shipping,
                    tax=host_tax, order_total=host_order_total)
     if host_summary else ''}
    {'<span id="submitOrderButtonId" data-testid="SPC_selectPlaceOrder">'
     '<input type="submit" name="placeYourOrder1" value="Place your order" '
     'title="Place your order"></span>' if host_place_order_button else ''}
    {overlay}
    """
    return _shell(body, title=f"Amazon.com: {title}")


def confirmation_page(
    *,
    order_number: str = "112-1234567-7654321",
    total: str = "117.94",
    delivery: str = "Arriving Thursday, September 24",
) -> str:
    """The thank-you page Amazon shows after a successful order."""
    body = f"""
    {_nav(True)}
    <div id="widget-purchaseConfirmationStatus">
      <h1 class="a-size-large">Order placed, thanks!</h1>
      <p>Confirmation will be sent to your email.</p>
    </div>
    <div id="order-summary">
      <span>Order #</span>
      <a href="/gp/css/order-details?orderID={order_number}">{order_number}</a>
      <span class="a-color-price">Order total: ${total}</span>
      <div class="delivery-promise">{delivery}</div>
    </div>
    """
    return _shell(body, title="Amazon.com Thanks You")


def confirmation_failed_page() -> str:
    """Amazon showing a problem instead of a confirmation."""
    body = f"""
    {_nav(True)}
    <div id="checkout-container">
      <div class="a-alert a-alert-error">
        <h4>There was a problem with your payment method</h4>
        <p>Please select another payment method to complete your order.</p>
      </div>
      <div id="payment-information">
        <div id="paymentMethodDisplay">Visa ending in 1234</div>
      </div>
    </div>
    """
    return _shell(body, title="Amazon.com Checkout")


def orders_history_page(
    *,
    order_number: str = "112-1234567-7654321",
    total: str = "117.94",
    title: str = DEFAULT_TITLE,
) -> str:
    """The order-history page, used to verify an order independently."""
    body = f"""
    {_nav(True)}
    <div class="js-order-card order-card">
      <div class="order-info">
        <span>ORDER PLACED</span><span>September 16, 2026</span>
        <span>TOTAL</span><span>${total}</span>
        <span>SHIP TO</span><span>John D.</span>
        <span>ORDER # {order_number}</span>
      </div>
      <div class="a-fixed-left-grid">
        <span class="a-link-normal">{title}</span>
        <span>Arriving Thursday, September 24</span>
      </div>
    </div>
    """
    return _shell(body, title="Your Orders")


# ---------------------------------------------------------------------------
# Interruption pages
# ---------------------------------------------------------------------------


def sign_in_page() -> str:
    body = """
    <div id="authportal-main-section">
      <form name="signIn" method="post" action="/ap/signin">
        <input type="email" id="ap_email" name="email">
        <input type="password" id="ap_password" name="password">
        <input id="signInSubmit" type="submit" value="Sign in">
      </form>
    </div>
    """
    return _shell(body, title="Amazon Sign-In")


def mfa_page() -> str:
    body = """
    <div id="authportal-main-section">
      <form id="auth-mfa-form" method="post">
        <input type="tel" id="auth-mfa-otpcode" name="otpCode" autocomplete="off">
        <label for="auth-mfa-remember-device">Don't ask for codes on this device</label>
        <input type="checkbox" id="auth-mfa-remember-device">
        <input id="auth-signin-button" type="submit" value="Sign in">
      </form>
    </div>
    """
    return _shell(body, title="Two-Step Verification")


def captcha_page() -> str:
    body = """
    <div class="a-container">
      <h4>Enter the characters you see below</h4>
      <p>Sorry, we just need to make sure you're not a robot.</p>
      <form method="get" action="/errors/validateCaptcha">
        <img src="/captcha/image.jpg" alt="captcha">
        <input id="captchacharacters" name="field-keywords" type="text">
        <button type="submit">Continue shopping</button>
      </form>
    </div>
    """
    return _shell(body, title="Amazon.com")


def service_error_page() -> str:
    """Amazon's throttle / generic failure page. Often served with HTTP 200."""
    body = """
    <div id="g">
      <a href="/"><img src="/dogs/rufus.jpg" alt="Sorry! Something went wrong!"></a>
      <h4>Sorry! Something went wrong on our end. Please go back and try again
      or go to Amazon's home page.</h4>
    </div>
    """
    return _shell(body, title="Sorry! Something went wrong!")


def add_to_cart_addon_sheet() -> str:
    """The protection-plan interstitial shown after adding to the cart."""
    body = f"""
    {_nav(True)}
    <div id="attach-warranty-pane" class="attach-sidesheet">
      <h2 id="protection-plan-title">Add a protection plan</h2>
      <div class="attach-warranty-option">
        <span>3-Year Protection Plan for $24.99</span>
        <input type="submit" value="Add protection">
      </div>
      <span id="attachSiNoCoverage" class="a-button">
        <input id="attachSiNoCoverage-announce" type="submit" value="No thanks">
      </span>
      <a id="attach-close_sideSheet-link" href="#">Close</a>
    </div>
    <div id="sw-atc-confirmation">
      <span>Added to Cart</span>
    </div>
    """
    return _shell(body, title="Amazon.com Shopping Cart")
