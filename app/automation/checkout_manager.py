"""Reading the checkout, submitting an order, and verifying the result.

This is the only module in the program that can click Amazon's order button,
and it will not do so unless handed a :class:`SubmitAuthorization`. That
object records, in one place, every precondition the click depends on: which
purchase job and attempt it belongs to, the total the user approved, that the
Purchase Guard passed, and that test mode is off. :meth:`CheckoutManager.submit`
re-checks all of them and refuses otherwise, so test mode cannot reach a
submission even if a caller asks it to.

Two ordering rules matter more than anything else here:

* **The submission is recorded before the click, never after.** If the
  process dies during the click, the record already exists, so startup
  recovery knows an order may have been placed. Recording afterwards would
  lose exactly the case that matters.

* **A click is not an order.** :meth:`read_confirmation` only reports success
  when Amazon's own confirmation was positively identified. When it cannot
  tell, it says so, and the caller must treat that as uncertain rather than
  retrying -- a retry after an ambiguous result is how duplicate orders
  happen.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Final, Sequence

from app.automation import selectors
from app.automation.page_reader import PageReader, clean
from app.core.errors import AppError, ErrorCode
from app.core.money import Money, extract_prices, parse_money_ceiling
from app.purchasing.models import (
    CartLine,
    CheckoutSnapshot,
    OrderConfirmation,
    normalise_label,
)

logger = logging.getLogger("app.automation.checkout")

#: How long to wait for Amazon to show a confirmation after submitting.
CONFIRMATION_TIMEOUT_MS = 90_000

#: How long to wait for the Buy Now modal's panel. Without waiting for it the
#: order silently fails and the page blanks, so this is a precondition.
TURBO_PANEL_TIMEOUT_MS = 20_000

_QUANTITY_PATTERN = re.compile(r"qty\s*:?\s*(\d+)", re.IGNORECASE)

#: "Nobody has looked yet", as distinct from "there is no modal". Only
#: needed because ``None`` is a meaningful answer for a frame.
_UNSET: Final = object()


@dataclass(frozen=True)
class SubmitAuthorization:
    """Permission to place one specific order.

    Constructed only by the purchase service, after the guard has passed and
    the user (in assisted mode) has confirmed. Every field is re-checked
    inside :meth:`CheckoutManager.submit`.
    """

    purchase_job_id: int
    attempt_id: int
    #: The total the user approved, or that the guard validated in automatic
    #: mode. Re-read and compared immediately before the click.
    approved_total: Money
    guard_passed: bool
    test_mode: bool
    #: Called immediately before the click, to persist that a submission is
    #: happening. If it raises, nothing is clicked.
    record_submission: Callable[[], None]

    def validate(self) -> None:
        """Raise unless this authorisation permits a real submission."""
        if self.test_mode:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                context={
                    "reason": "submit_attempted_in_test_mode",
                    "purchase_job_id": self.purchase_job_id,
                },
                detail_override=(
                    "Test mode is on, so no order was placed. Turn test mode "
                    "off in Settings to place real orders."
                ),
            )
        if not self.guard_passed:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                context={
                    "reason": "submit_attempted_without_passing_guard",
                    "purchase_job_id": self.purchase_job_id,
                },
                detail_override=(
                    "The safety checks had not passed, so no order was placed."
                ),
            )


def _is_a_value(text: str, labels: tuple[str, ...]) -> bool:
    """Whether ``text`` is an answer rather than the heading above one.

    Amazon puts "Payment method" and "Shipping address" in the same
    containers as the card and the address, so a chain that walks past an
    empty element lands on the heading and reports it as the value.
    """
    normalised = normalise_label(text) or ""
    return bool(normalised) and normalised not in labels


@dataclass(frozen=True)
class PlaceOrderControl:
    """The located order button."""

    locator: Any
    #: How many matches the chain found. Amazon's newer checkout renders two
    #: identical submit buttons, one above and one below the summary.
    matches: int
    in_turbo_frame: bool = False


class CheckoutManager:
    """Reads the checkout and, when authorised, submits the order."""

    # ---- reading ---------------------------------------------------------

    def read_checkout(self, reader: PageReader) -> CheckoutSnapshot:
        """Parse the order review into a :class:`CheckoutSnapshot`.

        Everything is read through one scope, resolved once: the Buy Now
        modal is a **separate document** inside ``#turbo-checkout-iframe``,
        and a reader bound to the host page finds none of it. Reading the
        host page there produced a snapshot with no total, no address, no
        payment and no lines -- which the guard correctly refused, so every
        Buy Now purchase blocked at the final check. Buy Now is the preferred
        cart-isolation strategy, so that made the preferred route unusable.
        """
        frame = self._turbo_frame(reader)
        if frame is not None:
            logger.info("Reading the checkout from inside the Buy Now modal")

        summary = self._read_summary(reader, frame)
        lines = self._read_lines(reader, frame)
        control = self.find_place_order(reader, frame=frame)

        snapshot = CheckoutSnapshot(
            lines=tuple(lines),
            item_subtotal=summary.get("items"),
            shipping=summary.get("shipping"),
            tax=summary.get("tax"),
            promotion=summary.get("promotion"),
            order_total=summary.get("total"),
            address_label=self._read_address(reader, frame),
            payment_label=self._read_payment(reader, frame),
            addons=self._detect_addons(lines),
            is_subscription=reader.exists(
                selectors.SUBSCRIPTION_AT_CHECKOUT, scope=frame
            ),
            place_order_control_found=control is not None,
            page_url=reader.url(),
        )

        logger.info(
            "Checkout read",
            extra={
                "lines": len(lines),
                "order_total_cents": (
                    snapshot.order_total.cents if snapshot.order_total else None
                ),
                "shipping_cents": snapshot.shipping.cents if snapshot.shipping else None,
                "tax_cents": snapshot.tax.cents if snapshot.tax else None,
                "has_address": bool(snapshot.address_label),
                "has_payment": bool(snapshot.payment_label),
                "addons": snapshot.addons,
                "subscription": snapshot.is_subscription,
                "order_button_found": snapshot.place_order_control_found,
                "selector_fallbacks": reader.fallbacks,
            },
        )
        return snapshot

    def _read_summary(
        self, reader: PageReader, scope: Any | None = None
    ) -> dict[str, Money]:
        """Read the order summary by matching each row's label text.

        Positional table access breaks whenever Amazon adds a row, and the id
        commonly cited for the order total could not be confirmed in current
        markup, so the label is the anchor.
        """
        found: dict[str, list[Money]] = {}
        root = scope if scope is not None else reader.page
        try:
            rows = root.locator(selectors.CHECKOUT_SUMMARY_ROWS)
            total_rows = rows.count()
        except Exception:  # noqa: BLE001
            return {}

        for index in range(min(total_rows, 40)):
            try:
                text = clean(rows.nth(index).text_content(timeout=1_500))
            except Exception:  # noqa: BLE001
                continue
            if not text:
                continue
            key = self._classify_summary_row(text)
            if key is None:
                continue
            # The whole row is parsed, never a fragment of it. Classifying on
            # the text before the first colon while taking the value from
            # after the *last* colon meant a row carrying a second colon --
            # fine print inside the grand total, say -- yielded the wrong
            # fragment, and could read the order total LOWER than the truth.
            # That defeats the point of the ceiling parser, which only
            # guarantees safety when it is shown the whole string.
            prices = extract_prices(text)
            if not prices:
                continue
            if len({price.cents for price in prices}) > 1:
                logger.warning(
                    "A summary row contained several prices; taking the largest",
                    extra={
                        "row": key,
                        "values": sorted(price.cents for price in prices),
                    },
                )
            found.setdefault(key, []).append(
                max(prices, key=lambda price: price.cents)
            )

        result: dict[str, Money] = {}
        for key, values in found.items():
            distinct = {value.cents for value in values}
            if len(distinct) > 1:
                # Amazon renders the grand total twice; differing values mean
                # the page was misread. Take the largest, which can only make
                # the guard stricter.
                logger.warning(
                    "Summary row appeared more than once with different values",
                    extra={"row": key, "values": sorted(distinct)},
                )
            result[key] = max(values, key=lambda money: money.cents)
        return result

    @staticmethod
    def _classify_summary_row(text: str) -> str | None:
        """Map a summary row's text to a known field, most specific first."""
        normalised = normalise_label(text) or ""
        label = normalised.split(":")[0].strip()
        if not label:
            return None

        def matches(candidates: Sequence[str]) -> bool:
            return any(label == candidate for candidate in candidates)

        # Order matters: "estimated tax to be collected" must not be read as
        # "total", and "order total" must not be read as "items".
        if matches(selectors.TAX_LABELS):
            return "tax"
        if matches(selectors.SHIPPING_LABELS):
            return "shipping"
        if matches(selectors.PROMOTION_LABELS):
            return "promotion"
        if matches(selectors.ORDER_TOTAL_LABELS):
            return "total"
        if matches(selectors.ITEM_SUBTOTAL_LABELS):
            return "items"
        return None

    def _read_lines(
        self, reader: PageReader, scope: Any | None = None
    ) -> list[CartLine]:
        root = scope if scope is not None else reader.page
        try:
            rows = root.locator(selectors.CHECKOUT_LINE_ITEMS)
            total = rows.count()
        except Exception:  # noqa: BLE001
            return []

        lines: list[CartLine] = []
        for index in range(min(total, 40)):
            row = rows.nth(index)
            asin = self._row_asin(row)
            try:
                text = clean(row.text_content(timeout=1_500)) or ""
            except Exception:  # noqa: BLE001
                text = ""
            if not text and not asin:
                continue

            quantity = self._line_quantity(row, text)
            unit_price = self._line_price(row)
            title = self._line_title(row) or (text[:120] or None)

            lines.append(
                CartLine(
                    asin=(asin or "").strip().upper() or None,
                    title=title,
                    quantity=quantity,
                    unit_price=unit_price,
                    line_price=(
                        unit_price * quantity
                        if unit_price is not None and quantity is not None
                        else None
                    ),
                    seller=self._line_seller(row),
                )
            )
        return lines

    @staticmethod
    def _row_asin(row: Any) -> str | None:
        """The ASIN of a checkout line, from the row or from inside it.

        The current layout puts ``data-asin`` on a node *within* the line
        rather than on the line itself, and the row's own id is a base64
        blob. A row whose ASIN cannot be found is treated as an unrelated
        item by the guard, so looking in both places is what stops an
        ordinary order being refused as somebody else's.
        """
        try:
            own = row.get_attribute("data-asin", timeout=1_500)
        except Exception:  # noqa: BLE001
            own = None
        if own:
            return own
        try:
            inner = row.locator(selectors.CHECKOUT_LINE_ASIN_NODE).first
            if inner.count() == 0:
                return None
            return inner.get_attribute("data-asin", timeout=1_500)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _line_quantity(row: Any, text: str) -> int | None:
        """The line's quantity, or ``None`` when it could not be read.

        Amazon words this several ways and sometimes renders a ``select``
        instead of text, so a form control is tried before the wording.
        Returning ``None`` rather than ``1`` matters: the guard validates
        whatever it is given, so a fabricated ``1`` would be reported as a
        PASS against a rule asking for one -- while the order actually
        contained three.
        """
        for selector in selectors.CHECKOUT_LINE_QUANTITY_CONTROLS:
            try:
                control = row.locator(selector).first
                if control.count() == 0:
                    continue
                raw = (control.input_value(timeout=1_500) or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if raw.isdigit() and int(raw) > 0:
                return int(raw)

        for pattern in selectors.CHECKOUT_QUANTITY_PATTERNS:
            match = pattern.search(text)
            if match:
                value = int(match.group(1))
                if value > 0:
                    return value
        logger.warning("A checkout line had no readable quantity")
        return None

    @staticmethod
    def _line_seller(row: Any) -> str | None:
        """The "Sold by" text on one checkout line, if Amazon shows it.

        Read so the guard can check the seller of the offer actually being
        bought, rather than trusting the product page read minutes earlier --
        the buybox can flip in between.
        """
        text: str | None = None
        for selector in selectors.CHECKOUT_LINE_SELLER:
            try:
                target = row.locator(selector).first
                if target.count() == 0:
                    continue
                text = clean(target.text_content(timeout=1_500))
            except Exception:  # noqa: BLE001
                continue
            if text:
                break

        if not text:
            # No element for it, but the row still says it in words:
            # "Ships from Amazon.com Sold by Aproca Direct".
            try:
                whole = clean(row.inner_text(timeout=1_500)) or ""
            except Exception:  # noqa: BLE001
                whole = ""
            match = selectors.SOLD_BY_PATTERN.search(whole)
            text = clean(match.group(1)) if match else None

        if not text:
            return None
        cleaned = text.strip()
        for prefix in ("sold by:", "sold by", "ships from and sold by"):
            if cleaned.lower().startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
                break
        result = clean(cleaned)
        return result[:120] if result else None

    @staticmethod
    def _line_price(row: Any) -> Money | None:
        for selector in selectors.CHECKOUT_LINE_PRICE:
            try:
                target = row.locator(selector).first
                if target.count() == 0:
                    continue
                value = parse_money_ceiling(
                    clean(target.text_content(timeout=1_500))
                )
                if value is not None:
                    return value
            except Exception:  # noqa: BLE001
                continue
        return None

    @staticmethod
    def _line_title(row: Any) -> str | None:
        for selector in selectors.CHECKOUT_LINE_TITLE:
            try:
                target = row.locator(selector).first
                if target.count() == 0:
                    continue
                text = clean(target.text_content(timeout=1_500))
                if text:
                    return text[:200]
            except Exception:  # noqa: BLE001
                continue
        return None

    def _read_address(
        self, reader: PageReader, scope: Any | None = None
    ) -> str | None:
        reading = reader.text(
            selectors.CHECKOUT_ADDRESS,
            scope=scope,
            visible_text=True,
            accept=lambda value: _is_a_value(value, selectors.NON_ADDRESS_LABELS),
        )
        if not reading.value:
            return None
        # The address block is multi-line; collapse it to a single readable
        # line for display and comparison.
        collapsed = clean(reading.value.replace("\n", ", "))
        if not collapsed:
            return None
        return collapsed[:200]

    def _read_payment(
        self, reader: PageReader, scope: Any | None = None
    ) -> str | None:
        reading = reader.text(
            selectors.CHECKOUT_PAYMENT,
            scope=scope,
            visible_text=True,
            accept=lambda value: _is_a_value(value, selectors.NON_PAYMENT_LABELS),
        )
        if not reading.value:
            return None
        return clean(reading.value)[:200] if clean(reading.value) else None

    @staticmethod
    def _detect_addons(lines: Sequence[CartLine]) -> tuple[str, ...]:
        """Identify extra items Amazon attached, by their wording.

        Protection plans and installation services appear as ordinary line
        items rather than in a dedicated container, so they are recognised by
        name.
        """
        found: list[str] = []
        for line in lines:
            haystack = normalise_label(line.title) or ""
            if any(marker in haystack for marker in selectors.ADDON_LINE_MARKERS):
                found.append(line.display_title)
        return tuple(found)

    # ---- the order button ------------------------------------------------

    def find_place_order(
        self, reader: PageReader, *, frame: Any | None = _UNSET
    ) -> PlaceOrderControl | None:
        """Locate Amazon's order button without clicking it.

        Used by test mode to prove the whole sequence works, and by a real
        submission immediately before the click.

        ``frame`` may be an already-resolved Buy Now modal, or ``None`` to say
        "there is no modal, do not look again". Resolving it costs a real wait
        for the panel to render, and a submission asks for the button twice,
        so the caller passes what it already knows.
        """
        turbo = self._turbo_frame(reader) if frame is _UNSET else frame
        if turbo is not None:
            locator, matches = self._find_in(turbo, selectors.TURBO_PLACE_ORDER)
            if locator is not None:
                return PlaceOrderControl(
                    locator=locator, matches=matches, in_turbo_frame=True
                )

        locator, resolution = reader.find(
            selectors.PLACE_ORDER_BUTTON, visible_only=True
        )
        if locator is None:
            locator, resolution = reader.find(selectors.PLACE_ORDER_BUTTON)
        if locator is None:
            return None
        matches = reader.count(selectors.PLACE_ORDER_BUTTON)
        if matches > 1:
            # Two identical buttons is normal on the newer checkout. Acting on
            # a specific one, rather than "whatever matched", keeps behaviour
            # predictable.
            logger.info(
                "Several order buttons present; using the first",
                extra={"matches": matches},
            )
        return PlaceOrderControl(locator=locator, matches=matches)

    def _turbo_frame(self, reader: PageReader) -> Any | None:
        """The Buy Now modal's frame, once its panel has rendered."""
        try:
            if reader.page.locator(selectors.TURBO_CHECKOUT_IFRAME).count() == 0:
                return None
            frame = reader.page.frame_locator(selectors.TURBO_CHECKOUT_IFRAME)
        except Exception:  # noqa: BLE001
            return None

        # The wait is a budget for the whole chain, not for each candidate.
        # Spending the full timeout per candidate meant a modal that never
        # rendered cost 40 seconds a look -- and a submission looks twice,
        # while advancing the checkout looks once per step.
        candidates = [c for c in selectors.TURBO_CHECKOUT_PANEL if c.css]
        budget = max(1_000, TURBO_PANEL_TIMEOUT_MS // max(1, len(candidates)))
        for candidate in candidates:
            try:
                panel = frame.locator(candidate.css).first
                panel.wait_for(state="attached", timeout=budget)
                logger.info("Buy Now checkout panel is ready")
                return frame
            except Exception:  # noqa: BLE001
                continue
        logger.warning("Buy Now opened a frame but its panel never appeared")
        return None

    @staticmethod
    def _find_in(scope: Any, chain: Any) -> tuple[Any | None, int]:
        for candidate in chain:
            if not candidate.css:
                continue
            try:
                locator = scope.locator(candidate.css)
                matches = locator.count()
            except Exception:  # noqa: BLE001
                continue
            if matches:
                return locator.first, matches
        return None, 0

    def continue_through_checkout(self, reader: PageReader, *, max_steps: int = 4) -> None:
        """Advance the newer multi-step checkout to the review page.

        Stops as soon as the order button is visible. Each step is re-checked
        against the page rather than assumed, because the number of steps
        depends on the account's saved address and payment details.
        """
        for step in range(max_steps):
            # The frame is re-resolved each step on purpose: clicking
            # "Continue" can be what opens the modal in the first place.
            if self.find_place_order(reader) is not None:
                return
            locator, _ = reader.find(selectors.CHECKOUT_CONTINUE, visible_only=True)
            if locator is None:
                return
            logger.info("Advancing checkout", extra={"step": step + 1})
            try:
                locator.click(timeout=10_000)
                reader.page.wait_for_load_state("domcontentloaded", timeout=30_000)
            except Exception as exc:  # noqa: BLE001
                raise AppError(
                    ErrorCode.CHECKOUT_CHANGED,
                    context={"reason": "continue_failed", "step": step + 1},
                    cause=exc,
                ) from exc

    # ---- submitting ------------------------------------------------------

    def submit(
        self, reader: PageReader, authorization: SubmitAuthorization
    ) -> None:
        """Place the order. The only place in the program that does.

        The sequence is deliberate and must not be reordered:

        1. Validate the authorisation (test mode off, guard passed).
        2. Re-read the checkout and confirm the total still matches what was
           approved -- consent was given for a specific amount.
        3. Locate the order button.
        4. Persist that a submission is happening.
        5. Click.

        Step 4 precedes step 5 so that a crash during the click still leaves
        evidence an order may exist.
        """
        authorization.validate()

        current = self.read_checkout(reader)
        if current.order_total is None:
            raise AppError(
                ErrorCode.CHECKOUT_TOTAL_CHANGED,
                context={"reason": "total_unreadable_at_submit"},
                detail_override=(
                    "The order total could not be re-checked immediately before "
                    "ordering, so nothing was ordered."
                ),
            )
        if (
            current.order_total.currency != authorization.approved_total.currency
            or current.order_total.cents != authorization.approved_total.cents
        ):
            raise AppError(
                ErrorCode.CHECKOUT_TOTAL_CHANGED,
                context={
                    "approved": authorization.approved_total.format(),
                    "current": current.order_total.format(),
                },
            )

        control = self.find_place_order(reader, frame=self._turbo_frame(reader))
        if control is None:
            raise AppError(
                ErrorCode.CHECKOUT_CHANGED,
                context={"reason": "order_button_missing_at_submit"},
                detail_override=(
                    "Amazon's order button could not be found, so nothing was "
                    "ordered."
                ),
            )

        # Recorded first: a crash between here and the click must still be
        # recoverable as "an order may exist".
        authorization.record_submission()

        logger.warning(
            "Submitting order",
            extra={
                "purchase_job_id": authorization.purchase_job_id,
                "attempt_id": authorization.attempt_id,
                "total_cents": current.order_total.cents,
                "in_turbo_frame": control.in_turbo_frame,
            },
        )
        control.locator.click(timeout=30_000, no_wait_after=True)

    def read_confirmation(self, reader: PageReader) -> OrderConfirmation:
        """Read Amazon's response to a submitted order.

        ``verified`` is True only when a confirmation was positively
        identified. Everything else -- a timeout, an unrecognised page, an
        error banner -- returns ``verified=False``, which the caller must
        treat as uncertain.
        """
        try:
            reader.page.wait_for_load_state(
                "domcontentloaded", timeout=CONFIRMATION_TIMEOUT_MS
            )
        except Exception:  # noqa: BLE001
            logger.warning("Timed out waiting for Amazon's response")

        locator, _ = reader.wait_for_any(
            selectors.CONFIRMATION_MARKERS, timeout_ms=30_000
        )
        url = reader.url()
        page_text = reader.page_text(limit=60_000)

        order_number = self._extract_order_number(url, page_text)
        text_confirms = bool(selectors.CONFIRMATION_TEXT_PATTERN.search(page_text))
        url_confirms = selectors.THANK_YOU_URL_FRAGMENT in url.lower()
        verified = bool(locator is not None or text_confirms or url_confirms)

        confirmation = OrderConfirmation(
            verified=verified,
            order_number=order_number,
            order_total=self._confirmation_total(page_text),
            delivery_estimate=self._confirmation_delivery(page_text),
            page_url=url,
        )
        logger.warning(
            "Order result read",
            extra={
                "verified": verified,
                "order_number": order_number,
                "url_confirms": url_confirms,
                "text_confirms": text_confirms,
                "marker_found": locator is not None,
            },
        )
        return confirmation

    @staticmethod
    def _extract_order_number(url: str, page_text: str) -> str | None:
        """The order number, from the URL if possible, then the page text.

        The thank-you URL carries it as ``purchaseId``, which is the most
        reliable source because it does not depend on page wording.
        """
        match = selectors.PURCHASE_ID_PATTERN.search(url)
        if match:
            return match.group(1)
        match = selectors.ORDER_NUMBER_PATTERN.search(page_text)
        return match.group(1) if match else None

    @staticmethod
    def _confirmation_total(page_text: str) -> Money | None:
        match = re.search(
            r"order total[:\s]*([^\n]{0,40})", page_text, re.IGNORECASE
        )
        return parse_money_ceiling(match.group(1)) if match else None

    @staticmethod
    def _confirmation_delivery(page_text: str) -> str | None:
        match = re.search(
            r"(arriving[^\n]{0,60}|delivery[^\n]{0,60})", page_text, re.IGNORECASE
        )
        return clean(match.group(1)) if match else None

    # ---- independent verification ----------------------------------------

    def verify_via_orders_page(
        self, reader: PageReader, *, expected_total: Money | None = None
    ) -> OrderConfirmation:
        """Look for the order on the order-history page.

        Used when the confirmation page could not be read. The history page
        renders a different template, so it is an independent check rather
        than a second attempt at the same one. This only ever *reads*; it can
        never place an order.
        """
        try:
            rows = reader.page.locator(selectors.ORDER_CARD)
            total = rows.count()
        except Exception:  # noqa: BLE001
            total = 0

        if total == 0:
            # The search-results variant of this page renders a single card
            # regardless of match count, so fall back to the whole body text.
            body = reader.page_text(limit=60_000)
            match = selectors.ORDER_CARD_NUMBER_PATTERN.search(
                body
            ) or selectors.ORDER_NUMBER_PATTERN.search(body)
            if match is None:
                return OrderConfirmation(verified=False, page_url=reader.url())
            return OrderConfirmation(
                verified=False,
                order_number=match.group(1),
                page_url=reader.url(),
            )

        for index in range(min(total, 10)):
            try:
                text = clean(rows.nth(index).inner_text(timeout=2_500)) or ""
            except Exception:  # noqa: BLE001
                continue
            match = selectors.ORDER_CARD_NUMBER_PATTERN.search(
                text
            ) or selectors.ORDER_NUMBER_PATTERN.search(text)
            if match is None:
                continue
            card_total = self._order_card_total(text)
            if (
                expected_total is not None
                and card_total is not None
                and card_total.cents != expected_total.cents
            ):
                # A different order. Keep looking rather than claiming this one.
                continue
            logger.info(
                "Order found on the orders page",
                extra={"order_number": match.group(1)},
            )
            return OrderConfirmation(
                verified=True,
                order_number=match.group(1),
                order_total=card_total,
                page_url=reader.url(),
            )

        return OrderConfirmation(verified=False, page_url=reader.url())

    @staticmethod
    def _order_card_total(text: str) -> Money | None:
        """The order total from an order-history card's text.

        Cards render as line-broken labelled blocks (``TOTAL`` then the
        amount). A cancelled order has no total at all, carrying the word
        ``Cancelled`` instead, so ``None`` is a normal answer.
        """
        labelled = re.search(
            r"total\s*[\r\n:]*\s*(\$?[\d.,]+)", text, re.IGNORECASE
        )
        if labelled:
            value = parse_money_ceiling(labelled.group(1))
            if value is not None:
                return value
        return parse_money_ceiling(text)


MANAGER = CheckoutManager()
