"""Reading a product page into a :class:`ProductSnapshot`.

The parser's contract is that it never guesses. Anything it cannot read
reliably comes back as ``None`` or ``UNKNOWN``, and the Purchase Guard turns
that into a blocked purchase. A parser that filled in a plausible default
would convert a layout change into an order at the wrong price.

Three specific hazards are handled explicitly:

* **The split price.** Amazon renders the visible price as
  ``<span class="a-price-whole">109.</span><span class="a-price-fraction">97</span>``.
  Reading the container's text and stripping non-digits yields ``10997``, a
  hundredfold error. The parser prefers the ``.a-offscreen`` screen-reader
  span, which holds one clean value, and only falls back to the split nodes
  through :func:`app.core.money.parse_split_price`.

* **The struck-through list price.** It sits next to the real price in the
  same block. Selectors exclude ``.a-text-price``, and where noisy text must
  be parsed the ceiling parser is used, which returns the largest value found
  so a mis-read can only block a purchase, never overspend.

* **Pre-selected Subscribe & Save.** On consumables the recurring option can
  be the default, in which case Add to Cart starts a subscription. The parser
  reports which accordion row is active; an inability to prove the one-time
  row is selected is reported as a pre-selected subscription, which blocks.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.automation import selectors
from app.automation.page_reader import PageReader, clean
from app.core.money import Money, parse_money, parse_money_ceiling, parse_split_price
from app.purchasing.models import (
    Availability,
    ItemCondition,
    ProductSnapshot,
    VariationSnapshot,
    normalise_label,
)

logger = logging.getLogger("app.automation.product")

#: Wording that means an item cannot be bought right now.
_OUT_OF_STOCK_PHRASES = (
    "currently unavailable",
    "out of stock",
    "temporarily out of stock",
    "unavailable",
    "we don't know when or if this item will be back",
    "no featured offers available",
    "see all buying options",
)

#: Wording that means it can.
_IN_STOCK_PHRASES = (
    "in stock",
    "only 1 left",
    "only 2 left",
    "left in stock",
    "available",
    "usually ships",
    "ships within",
)

_PREORDER_PHRASES = ("pre-order", "preorder", "available for pre-order")

#: A label like ``Colour:`` before a variation's value.
_LABEL_SUFFIX = re.compile(r"\s*:\s*$")


class ProductParser:
    """Turns a loaded product page into a snapshot."""

    def parse(
        self,
        reader: PageReader,
        *,
        expected_asin: str | None = None,
        marketplace: str = selectors.DEFAULT_MARKETPLACE,
    ) -> ProductSnapshot:
        """Read everything the guard needs from the current page."""
        asin = self._read_asin(reader) or (expected_asin or "").strip().upper()
        price, price_was_loose = self._read_price(reader)
        availability, availability_text = self._read_availability(reader)
        seller = self._read_seller(reader)
        ships_from = self._read_text(reader, selectors.PRODUCT_SHIPS_FROM)
        subscription_preselected = self.read_subscription_preselected(reader)

        snapshot = ProductSnapshot(
            asin=asin,
            marketplace=marketplace,
            title=self._read_text(reader, selectors.PRODUCT_TITLE),
            brand=self._read_brand(reader),
            image_url=self._read_image(reader),
            url=self._read_canonical(reader) or reader.url() or None,
            price=price,
            list_price=self._read_list_price(reader, price),
            availability=availability,
            availability_text=availability_text,
            seller=seller,
            ships_from=ships_from,
            condition=self._read_condition(reader),
            variation=self._read_variation(reader),
            variation_picker_present=self._has_variation_picker(reader),
            max_quantity=self._read_max_quantity(reader),
            prime_eligible=self._read_prime(reader),
            delivery_estimate=self._read_text(
                reader, selectors.PRODUCT_DELIVERY_ESTIMATE
            ),
            subscription_preselected=subscription_preselected,
            buy_now_available=reader.exists(selectors.BUY_NOW_BUTTON),
            add_to_cart_available=reader.exists(selectors.ADD_TO_CART_BUTTON),
        )

        logger.info(
            "Product read",
            extra={
                "asin": snapshot.asin,
                "price_cents": snapshot.price.cents if snapshot.price else None,
                "availability": snapshot.availability.value,
                "seller": snapshot.seller,
                "condition": snapshot.condition.value,
                "variation": snapshot.variation.describe_full(),
                "price_from_fallback": price_was_loose,
                "selector_fallbacks": reader.fallbacks,
            },
        )
        return snapshot

    # ---- identity --------------------------------------------------------

    def _read_asin(self, reader: PageReader) -> str | None:
        reading = reader.input_value(selectors.PRODUCT_ASIN_INPUT)
        if reading.value:
            candidate = reading.value.strip().upper()
            if selectors.ASIN_PATTERN.match(candidate):
                return candidate

        canonical = self._read_canonical(reader)
        from_canonical = selectors.extract_asin(canonical)
        if from_canonical:
            return from_canonical

        return selectors.extract_asin(reader.url())

    def _read_canonical(self, reader: PageReader) -> str | None:
        return reader.attribute(selectors.CANONICAL_LINK, "href").value

    def _read_text(self, reader: PageReader, chain: Any) -> str | None:
        return reader.text(chain).value

    def _read_brand(self, reader: PageReader) -> str | None:
        raw = self._read_text(reader, selectors.PRODUCT_BRAND)
        if not raw:
            return None
        # Amazon's byline reads "Visit the Klein Tools Store" or "Brand: Klein".
        cleaned = re.sub(
            r"^(?:visit the|brand:?|by)\s+", "", raw, flags=re.IGNORECASE
        ).strip()
        cleaned = re.sub(r"\s+store$", "", cleaned, flags=re.IGNORECASE).strip()
        return cleaned or None

    def _read_image(self, reader: PageReader) -> str | None:
        for attribute in ("src", "data-old-hires", "data-a-dynamic-image"):
            reading = reader.attribute(selectors.PRODUCT_IMAGE, attribute)
            value = reading.value
            if not value:
                continue
            if attribute == "data-a-dynamic-image":
                match = re.search(r'"(https?://[^"]+)"', value)
                if match:
                    return match.group(1)
                continue
            if value.startswith("http"):
                return value
        return None

    # ---- price -----------------------------------------------------------

    def _read_price(self, reader: PageReader) -> tuple[Money | None, bool]:
        """The buybox price, and whether a fallback route was needed."""
        locator, resolution = reader.find(selectors.PRODUCT_PRICE)
        if locator is not None:
            reading = reader.text(selectors.PRODUCT_PRICE)
            if reading.value:
                # A loose candidate may have matched a neighbouring list
                # price, so noisy text is parsed with the ceiling parser.
                parser = parse_money_ceiling if reading.loose else parse_money
                price = parser(reading.value)
                if price is None:
                    price = parse_money_ceiling(reading.value)
                if price is not None:
                    return price, bool(resolution and resolution.used_fallback)

        whole = reader.text(selectors.PRODUCT_PRICE_WHOLE).value
        fraction = reader.text(selectors.PRODUCT_PRICE_FRACTION).value
        if whole:
            price = parse_split_price(whole, fraction)
            if price is not None:
                logger.info(
                    "Price read from the split visible markup",
                    extra={"whole": whole, "fraction": fraction},
                )
                return price, True

        return None, False

    def _read_list_price(self, reader: PageReader, price: Money | None) -> Money | None:
        """The struck-through "was" price, or ``None``.

        A list price is only a list price if it is above what is being
        charged. Anything at or below the current price is something else the
        markup happened to share a class with -- a per-unit price, or the
        price itself -- and reporting it would show the user a saving that
        does not exist. It is display-only either way: no rule reads it.
        """
        reading = reader.text(selectors.PRODUCT_LIST_PRICE)
        if not reading.value:
            return None
        listed = parse_money_ceiling(reading.value)
        if listed is None or price is None:
            return listed
        if listed.currency != price.currency or listed <= price:
            logger.info(
                "Ignoring a list price that is not above the price",
                extra={"list_cents": listed.cents, "price_cents": price.cents},
            )
            return None
        return listed

    # ---- availability ----------------------------------------------------

    def _read_availability(
        self, reader: PageReader
    ) -> tuple[Availability, str | None]:
        if reader.exists(selectors.OUT_OF_STOCK_MARKERS):
            text = self._read_text(reader, selectors.PRODUCT_AVAILABILITY)
            return Availability.OUT_OF_STOCK, text or "Currently unavailable"

        text = self._read_text(reader, selectors.PRODUCT_AVAILABILITY)
        normalised = normalise_label(text) or ""

        if normalised:
            if any(phrase in normalised for phrase in _PREORDER_PHRASES):
                return Availability.PREORDER, text
            if any(phrase in normalised for phrase in _OUT_OF_STOCK_PHRASES):
                return Availability.OUT_OF_STOCK, text
            if any(phrase in normalised for phrase in _IN_STOCK_PHRASES):
                return Availability.IN_STOCK, text

        # No usable availability wording. Fall back to whether a purchase
        # control exists at all, which is the strongest remaining signal --
        # and report UNKNOWN when even that is absent, so the guard blocks.
        if reader.exists(selectors.ADD_TO_CART_BUTTON) or reader.exists(
            selectors.BUY_NOW_BUTTON
        ):
            return Availability.IN_STOCK, text or "Available"
        return Availability.UNKNOWN, text

    # ---- offer details ---------------------------------------------------

    def _read_seller(self, reader: PageReader) -> str | None:
        raw = self._read_text(reader, selectors.PRODUCT_SELLER)
        if not raw:
            return None
        # The loose "#merchant-info" candidate returns a whole sentence such
        # as "Ships from and sold by Amazon.com." -- extract the seller.
        match = re.search(
            r"sold by\s+(.+?)(?:\.|$|\s+and ships)", raw, flags=re.IGNORECASE
        )
        if match:
            return clean(match.group(1))
        if len(raw) > 80:
            # Too long to be a seller name; refuse rather than store a
            # sentence that would never match an expected seller.
            logger.info("Seller text was not a name", extra={"length": len(raw)})
            return None
        return raw

    def _read_condition(self, reader: PageReader) -> ItemCondition:
        """The condition of the offer in the buy box.

        Of the offer that would be **bought**, not of anything the page
        mentions. Amazon advertises a cheaper used copy inside the same buy
        box, and an earlier version of this method searched the page for
        "used", found that advertisement and reported "condition not stated"
        -- which made a New-only rule refuse an ordinary new item. A live run
        on 2026-09-16 blocked on exactly that.

        Order matters: the active accordion row is Amazon's own statement of
        which offer is selected, so it outranks both an explicit label
        elsewhere on the page and any wording search.
        """
        active = self._condition_from_active_offer(reader)
        if active is not ItemCondition.UNKNOWN:
            return active

        text = self._read_text(reader, selectors.PRODUCT_CONDITION)
        if text:
            parsed = ItemCondition.parse(text)
            if parsed is not ItemCondition.UNKNOWN:
                return parsed

        # A single-offer buy box states nothing at all when the offer is new.
        # The absence of used wording is meaningful there, but only inside the
        # buy box and only once the alternative offers have been taken out.
        haystack = self._buy_box_text_without_alternatives(reader)
        if haystack is None:
            # Nothing that could be called a buy box: do not guess.
            return ItemCondition.UNKNOWN
        if any(phrase in haystack for phrase in selectors.USED_CONDITION_PHRASES):
            return ItemCondition.UNKNOWN
        if reader.exists(selectors.ADD_TO_CART_BUTTON) or reader.exists(
            selectors.BUY_NOW_BUTTON
        ):
            return ItemCondition.NEW
        return ItemCondition.UNKNOWN

    @staticmethod
    def _visible_text(locator: Any) -> str:
        """The text a person would see, not the DOM's text content.

        Amazon inlines a dozen ``<style>`` elements inside the buy-box
        accordion rows, and ``text_content()`` returns their CSS along with
        the caption. Everywhere else in this parser ``text_content`` is the
        right choice -- it is what makes the offscreen price readable -- but
        here the visible caption is precisely the thing being read.
        """
        for reader_name in ("inner_text", "text_content"):
            try:
                raw = getattr(locator, reader_name)(timeout=2_000)
            except Exception:  # noqa: BLE001
                continue
            cleaned = normalise_label(raw)
            if cleaned:
                return cleaned
        return ""

    def _condition_from_active_offer(self, reader: PageReader) -> ItemCondition:
        """Read the condition off the selected buy-box row, if there is one."""
        locator, _ = reader.find(selectors.BUYBOX_ACTIVE_OFFER)
        if locator is None:
            return ItemCondition.UNKNOWN
        caption = self._visible_text(locator)
        if not caption:
            return ItemCondition.UNKNOWN

        # Used first: "Used - Like New" contains the word "new".
        if any(phrase in caption for phrase in selectors.USED_CONDITION_PHRASES):
            parsed = ItemCondition.parse(caption)
            return parsed if parsed is not ItemCondition.UNKNOWN else ItemCondition.USED
        if "buy new" in caption or caption.startswith("new"):
            return ItemCondition.NEW
        return ItemCondition.UNKNOWN

    def _buy_box_text_without_alternatives(self, reader: PageReader) -> str | None:
        """Buy-box text with the alternative-condition offers removed."""
        box, _ = reader.find(selectors.BUY_BOX_CONTAINER)
        if box is None:
            return None
        haystack = self._visible_text(box)
        if not haystack:
            return None
        for candidate in selectors.ALTERNATIVE_OFFER_BLOCKS:
            if not candidate.css:
                continue
            try:
                blocks = reader.page.locator(candidate.css)
                total = blocks.count()
            except Exception:  # noqa: BLE001
                continue
            for index in range(min(total, 4)):
                other = self._visible_text(blocks.nth(index))
                if other:
                    haystack = haystack.replace(other, " ")
        return haystack

    def _read_max_quantity(self, reader: PageReader) -> int | None:
        locator, _ = reader.find(selectors.PRODUCT_QUANTITY_SELECT)
        if locator is None:
            return None
        try:
            options = locator.locator("option")
            total = options.count()
        except Exception:  # noqa: BLE001
            return None
        if not total:
            return None
        values: list[int] = []
        for index in range(min(total, 60)):
            try:
                raw = options.nth(index).get_attribute("value", timeout=1_000)
            except Exception:  # noqa: BLE001
                continue
            if raw and raw.isdigit():
                values.append(int(raw))
        return max(values) if values else None

    def _read_prime(self, reader: PageReader) -> bool | None:
        """Prime eligibility, or ``None`` when it could not be determined.

        ``None`` is a real answer here, not a failure to try: Prime badges
        are inconsistently rendered, and the guard treats "required but
        undeterminable" as a block rather than assuming either way.
        """
        if reader.exists(selectors.PRIME_BADGE):
            return True
        page_text = normalise_label(reader.page_text(limit=20_000)) or ""
        if "prime" in page_text:
            # The word appears in navigation and adverts on every page, so its
            # mere presence proves nothing about this offer.
            return None
        return False

    def read_subscription_preselected(self, reader: PageReader) -> bool:
        """Whether Amazon has pre-selected a recurring delivery.

        Returns ``True`` when a Subscribe & Save row exists and the one-time
        row cannot be shown to be the active one. Being unable to tell is
        reported as pre-selected, because the consequence of getting this
        wrong is an unwanted recurring subscription.
        """
        sns_locator, _ = reader.find(selectors.SUBSCRIBE_AND_SAVE_ROW)
        if sns_locator is None:
            # The row's element ids are Amazon's, and they change. Wording
            # inside the buy box is the backstop: if a recurring delivery is
            # being offered there, "not a subscription" may only be concluded
            # from a one-time option that is positively selected.
            if not self._buy_box_offers_a_subscription(reader):
                return False
            logger.warning(
                "Subscription wording in the buy box with no Subscribe & Save "
                "row matched; treating the selectors as stale"
            )
            return not self._one_time_is_selected(reader)

        if self._one_time_is_selected(reader):
            return False

        try:
            sns_classes = sns_locator.get_attribute("class", timeout=2_000) or ""
        except Exception:  # noqa: BLE001
            sns_classes = ""
        if "a-accordion-active" in sns_classes:
            return True

        logger.info("Could not confirm the one-time purchase option was selected")
        return True

    @staticmethod
    def _buy_box_offers_a_subscription(reader: PageReader) -> bool:
        """Whether the buy box mentions a recurring delivery.

        Scoped to the buy box on purpose: the same phrases appear in
        recommendation strips on pages that have no subscription option at
        all, and treating those as subscriptions would block ordinary
        purchases. When the buy box cannot be found, the whole page is used,
        because being unable to look is not evidence of absence.
        """
        reading = reader.text(selectors.BUY_BOX_CONTAINER)
        text = normalise_label(reading.value) if reading.value else ""
        if not text:
            text = normalise_label(reader.page_text(limit=40_000))
        return any(phrase in text for phrase in selectors.SUBSCRIPTION_TEXT)

    def _one_time_is_selected(self, reader: PageReader) -> bool:
        """Whether the one-time purchase option is demonstrably the active one."""
        one_time, _ = reader.find(selectors.ONE_TIME_PURCHASE_ROW)
        if one_time is None:
            logger.info("No one-time purchase row was found")
            return False

        try:
            classes = one_time.get_attribute("class", timeout=2_000) or ""
        except Exception:  # noqa: BLE001
            classes = ""
        if "a-accordion-active" in classes:
            return True

        radio, _ = reader.find(selectors.ONE_TIME_PURCHASE_RADIO)
        if radio is not None:
            try:
                return bool(radio.is_checked(timeout=2_000))
            except Exception:  # noqa: BLE001
                return False
        return False

    # ---- variation -------------------------------------------------------

    def _read_variation(self, reader: PageReader) -> VariationSnapshot:
        """The selected variation, from either twister generation."""
        dimensions = self._read_legacy_twister(reader)
        if not dimensions:
            dimensions = self._read_inline_twister(reader)
        return VariationSnapshot(dimensions=dimensions)

    def _has_variation_picker(self, reader: PageReader) -> bool:
        """Whether the page offers variations at all.

        Kept separate from reading them, because "no variations" and "several
        variations, none of which could be read" are different answers and
        only the second one should worry anybody.

        The container alone is not evidence. A live amazon.com page for a
        product with no variations still contains an **empty**
        ``#twister_feature_div`` -- 82 bytes of whitespace, no children --
        so treating the container as a picker reported every ordinary product
        as having unreadable versions. Rows first; the container only counts
        when it actually has something in it.
        """
        for selector in (
            selectors.TWISTER_LEGACY_ROWS,
            selectors.TWISTER_INLINE_ROWS,
        ):
            try:
                if reader.page.locator(selector).count():
                    return True
            except Exception:  # noqa: BLE001 - a picker we cannot look for
                continue

        container, _ = reader.find(selectors.TWISTER_CONTAINER)
        if container is None:
            return False
        try:
            return bool(clean(container.text_content(timeout=2_000)))
        except Exception:  # noqa: BLE001
            return False

    def _read_legacy_twister(self, reader: PageReader) -> dict[str, str]:
        found: dict[str, str] = {}
        try:
            rows = reader.page.locator(selectors.TWISTER_LEGACY_ROWS)
            total = rows.count()
        except Exception:  # noqa: BLE001
            return found
        for index in range(min(total, 12)):
            row = rows.nth(index)
            label = self._row_text(row, selectors.TWISTER_LEGACY_LABEL)
            value = self._row_text(row, selectors.TWISTER_LEGACY_VALUE)
            self._add_dimension(found, label, value)
        return found

    def _read_inline_twister(self, reader: PageReader) -> dict[str, str]:
        found: dict[str, str] = {}
        try:
            rows = reader.page.locator(selectors.TWISTER_INLINE_ROWS)
            total = rows.count()
        except Exception:  # noqa: BLE001
            return found
        for index in range(min(total, 12)):
            row = rows.nth(index)
            label = self._row_text(row, selectors.TWISTER_INLINE_LABEL)
            if not label:
                label = self._label_from_row_id(row)
            value = self._row_text(row, selectors.TWISTER_INLINE_SELECTED)
            self._add_dimension(found, label, value)
        return found

    @staticmethod
    def _row_text(row: Any, selector: str) -> str | None:
        try:
            target = row.locator(selector).first
            if target.count() == 0:
                return None
            return clean(target.text_content(timeout=1_500))
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _label_from_row_id(row: Any) -> str | None:
        try:
            row_id = row.get_attribute("id", timeout=1_000) or ""
        except Exception:  # noqa: BLE001
            return None
        match = re.search(r"inline-twister-row-(.+?)(?:_name)?$", row_id)
        if not match:
            return None
        return match.group(1).replace("_", " ").replace("-", " ").strip().title()

    @staticmethod
    def _add_dimension(
        found: dict[str, str], label: str | None, value: str | None
    ) -> None:
        """Record a dimension, ignoring anything that is not one.

        Amazon puts non-variation labels in the same markup ("Quantity:",
        "Pattern Name:" when unset), so the label is checked against the
        known dimension names before it becomes part of the fingerprint the
        guard compares.
        """
        if not label or not value:
            return
        cleaned_label = _LABEL_SUFFIX.sub("", label).strip()
        normalised = normalise_label(cleaned_label) or ""
        normalised = re.sub(r"\s*name$", "", normalised).strip()
        if not normalised or normalised in {"quantity", "select"}:
            return
        if normalised not in selectors.KNOWN_VARIATION_LABELS:
            # Dropped rather than compared: Amazon puts non-dimension labels
            # in this markup, and treating those as dimensions would block
            # ordinary purchases. The consequence is that a genuine dimension
            # with an unfamiliar name carries no expectation of its own --
            # which the per-variation ASIN check still catches, because a
            # different variation is a different ASIN. Logged at INFO, not
            # DEBUG, so a stale list is visible in an ordinary log rather
            # than only under diagnostics.
            logger.info(
                "Ignoring an unrecognised variation label",
                extra={"label": cleaned_label},
            )
            return
        cleaned_value = clean(value)
        if not cleaned_value or len(cleaned_value) > 120:
            return
        found.setdefault(cleaned_label.rstrip(":").strip(), cleaned_value)


PARSER = ProductParser()
