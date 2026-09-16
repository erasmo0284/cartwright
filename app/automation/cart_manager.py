"""Reading the cart, and isolating one item for purchase.

Cart isolation is the difference between buying what the user asked for and
buying whatever happened to be sitting in their Amazon cart. Amazon's
"Proceed to checkout" takes the *whole* active cart, so the program must
never reach checkout from a cart that holds anything else.

Three strategies, in order of preference:

1. **Buy Now.** Creates a separate one-item checkout that bypasses the cart
   entirely, so a full cart is simply irrelevant. Preferred whenever the
   control is present. It is not offered for every offer, so it cannot be the
   only route.
2. **Empty cart.** If the cart is already empty, adding the item makes it the
   only thing there.
3. **Set aside the others.** With the user's explicit permission, move every
   unrelated line to "Saved for later", verify the cart is down to the target
   alone, buy, then move them back. Every move is journalled first, so an
   interruption can be undone rather than silently leaving the cart mutated.

If none applies, the purchase stops. The program never proceeds through a
checkout containing items it did not put there.

Two details from Amazon's actual markup shape this module:

* Line items are read only from inside the active-items container. The cart
  page also renders an "Items you may like" strip whose elements carry
  ``data-asin``, so a page-wide scrape returns products for a cart that is
  completely empty.
* On a genuinely empty cart the subtotal elements are absent from the DOM
  rather than present and blank, so a missing subtotal is an expected state
  and not a parse failure.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.automation import selectors
from app.automation.page_reader import PageReader, clean
from app.core.errors import AppError, ErrorCode
from app.core.money import Money, parse_money_ceiling
from app.purchasing.models import (
    CartLine,
    CartState,
    CartStrategy,
    ProductSnapshot,
    PurchaseRules,
    normalise_label,
)

logger = logging.getLogger("app.automation.cart")


@dataclass
class IsolationJournal:
    """A record of what was moved, so it can be put back.

    Persisted to the purchase job before any cart change is made. If the
    application is interrupted mid-sequence, the journal is what lets the
    user's cart be restored rather than left altered.
    """

    strategy: CartStrategy = CartStrategy.BUY_NOW
    #: ASIN and quantity of each line set aside, recorded before moving it.
    set_aside: list[dict[str, Any]] = field(default_factory=list)
    restored: bool = False

    def to_json(self) -> str:
        return json.dumps(
            {
                "strategy": self.strategy.value,
                "set_aside": self.set_aside,
                "restored": self.restored,
            }
        )

    @classmethod
    def from_json(cls, text: str | None) -> IsolationJournal:
        if not text:
            return cls()
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls.from_mapping(data)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> IsolationJournal:
        """Rebuild from the already-parsed mapping a database row yields."""
        if not data:
            return cls()
        journal = cls(restored=bool(data.get("restored")))
        try:
            journal.strategy = CartStrategy(data.get("strategy", "buy_now"))
        except ValueError:
            journal.strategy = CartStrategy.BUY_NOW
        raw = data.get("set_aside")
        if isinstance(raw, list):
            journal.set_aside = [item for item in raw if isinstance(item, dict)]
        return journal

    @property
    def has_pending_restore(self) -> bool:
        return bool(self.set_aside) and not self.restored


@dataclass(frozen=True)
class IsolationPlan:
    """How this purchase will be kept separate from the existing cart."""

    strategy: CartStrategy
    #: Lines that would have to be set aside for the cart route to be safe.
    foreign_lines: tuple[CartLine, ...] = ()
    reason: str = ""

    @property
    def needs_permission(self) -> bool:
        return self.strategy is CartStrategy.SET_ASIDE_OTHERS

    @property
    def is_blocked(self) -> bool:
        return self.strategy is CartStrategy.BLOCKED

    def describe(self) -> str:
        return {
            CartStrategy.BUY_NOW: "Buy Now will be used, so your cart is not touched.",
            CartStrategy.EMPTY_CART: "Your cart is empty, so only this item will be ordered.",
            CartStrategy.SET_ASIDE_OTHERS: (
                "Your cart holds other items. They will be moved to "
                "Saved for later, then put back after the order."
            ),
            CartStrategy.BLOCKED: self.reason
            or "Your cart could not be made safe to order from.",
        }[self.strategy]


class CartManager:
    """Reads the cart and prepares an isolated checkout."""

    # ---- reading ---------------------------------------------------------

    def read_cart(self, reader: PageReader) -> CartState:
        """Parse the cart page into a :class:`CartState`.

        The empty check comes first and is authoritative: Amazon's own
        "your cart is empty" message is a stronger signal than an absence of
        parsed rows, which could equally mean the selectors failed.
        """
        # "Empty" is only believed when no line rows exist. Reporting an
        # empty cart that in fact holds items is the single reading that
        # could let an unrelated item be checked out, so the rows are counted
        # before the wording is trusted.
        page_text = normalise_label(reader.page_text(limit=40_000)) or ""
        says_empty = any(phrase in page_text for phrase in selectors.CART_EMPTY_TEXT)
        shows_empty = reader.exists(
            selectors.CART_EMPTY_MARKERS, visible_only=True
        )

        container, resolution = reader.find(selectors.CART_ACTIVE_CONTAINER)
        line_count = 0
        if container is not None:
            try:
                line_count = int(container.locator(selectors.CART_LINE_ITEMS).count())
            except Exception:  # noqa: BLE001
                line_count = 0

        if (says_empty or shows_empty) and line_count == 0:
            logger.info(
                "Cart is empty",
                extra={"visible_marker": shows_empty, "wording": says_empty},
            )
            return CartState(lines=(), subtotal=None, reported_empty=True)

        if says_empty and line_count:
            logger.warning(
                "The cart claimed to be empty but has line items; parsing them",
                extra={"lines": line_count},
            )

        if container is None:
            raise AppError(
                ErrorCode.UNEXPECTED_PAGE,
                context={"reason": "cart_container_not_found"},
                detail_override=(
                    "The cart page did not contain the item list the app needs "
                    "to read, so nothing was ordered."
                ),
            )
        if resolution is not None and resolution.loose:
            logger.warning(
                "Cart container matched only a loose selector",
                extra={"candidate": resolution.candidate.describe()},
            )

        lines = self._read_lines(container)
        subtotal = parse_money_ceiling(reader.text(selectors.CART_SUBTOTAL).value)
        saved_count = self._count_saved(reader)

        logger.info(
            "Cart read",
            extra={
                "lines": len(lines),
                "units": sum(line.units for line in lines),
                "subtotal_cents": subtotal.cents if subtotal else None,
                "saved_for_later": saved_count,
            },
        )
        return CartState(
            lines=tuple(lines),
            subtotal=subtotal,
            saved_for_later_count=saved_count,
            reported_empty=False,
        )

    def _read_lines(self, container: Any) -> list[CartLine]:
        try:
            rows = container.locator(selectors.CART_LINE_ITEMS)
            total = rows.count()
        except Exception:  # noqa: BLE001
            return []

        lines: list[CartLine] = []
        for index in range(min(total, 60)):
            row = rows.nth(index)
            try:
                asin = row.get_attribute("data-asin", timeout=1_500)
            except Exception:  # noqa: BLE001
                asin = None
            title = self._row_text(row, selectors.CART_ITEM_TITLE)
            price_text = self._row_text(row, selectors.CART_ITEM_PRICE)
            seller_text = self._row_text(row, selectors.CART_ITEM_SELLER)
            try:
                row_text = clean(row.text_content(timeout=1_500)) or ""
            except Exception:  # noqa: BLE001
                row_text = ""
            quantity = self._row_quantity(row, row_text)
            row_id = self._row_id(row)

            unit_price = parse_money_ceiling(price_text)
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
                    row_id=row_id,
                    seller=self._clean_seller(seller_text),
                )
            )
        return lines

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
    def _clean_seller(text: str | None) -> str | None:
        if not text:
            return None
        cleaned = text.strip()
        for prefix in ("sold by:", "sold by", "ships from and sold by"):
            if cleaned.lower().startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
                break
        return clean(cleaned)

    @staticmethod
    def _row_quantity(row: Any, text: str | None = None) -> int | None:
        """The line's quantity, or ``None`` when it could not be read.

        Never 1 by default. This used to return 1 whenever the control was
        missing or unreadable, on the reasoning that the guard would then
        compare it against the expectation -- which is true only when the
        user asked for a different number. For the common rule "quantity 1",
        a fabricated 1 is reported as a PASS on a number nobody observed.
        ``None`` fails the check instead, which is the same contract the
        checkout reader follows.

        The form control is tried first, then the wording, because Amazon
        renders the compact cart with "Qty: 2" as text and no control.
        """
        raw = ""
        try:
            control = row.locator(selectors.CART_ITEM_QUANTITY_INPUT).first
            if control.count() > 0:
                raw = (control.input_value(timeout=1_500) or "").strip()
        except Exception:  # noqa: BLE001
            raw = ""
        if raw.isdigit() and int(raw) > 0:
            return int(raw)

        for pattern in selectors.CHECKOUT_QUANTITY_PATTERNS:
            match = pattern.search(text or "")
            if match:
                value = int(match.group(1))
                if value > 0:
                    return value

        logger.warning("A cart line had no readable quantity")
        return None

    @staticmethod
    def _row_id(row: Any) -> str | None:
        try:
            raw = row.get_attribute("id", timeout=1_000) or ""
        except Exception:  # noqa: BLE001
            return None
        return raw.removeprefix("sc-active-") or None

    @staticmethod
    def _count_saved(reader: PageReader) -> int | None:
        """How many items are saved for later, or ``None`` if unreadable.

        ``0`` and "could not be read" are different answers: the restore
        step reports how many of the user's own items were put back, and
        saying "none were saved" when the section could not be read would
        make a failed restore look like nothing to restore.
        """
        container, _ = reader.find(selectors.SAVED_FOR_LATER_CONTAINER)
        if container is None:
            # No section at all is a real answer: nothing is saved.
            return 0
        try:
            return int(container.locator(selectors.SAVED_FOR_LATER_ITEMS).count())
        except Exception:  # noqa: BLE001
            logger.warning("Could not count the saved-for-later items")
            return None

    # ---- planning --------------------------------------------------------

    def plan_isolation(
        self,
        *,
        product: ProductSnapshot,
        rules: PurchaseRules,
        cart: CartState,
        allow_set_aside: bool,
    ) -> IsolationPlan:
        """Choose how to keep this purchase separate from the existing cart."""
        if product.buy_now_available:
            return IsolationPlan(strategy=CartStrategy.BUY_NOW)

        foreign = cart.foreign_lines(rules.expected_asin)
        if not foreign:
            return IsolationPlan(strategy=CartStrategy.EMPTY_CART)

        if not product.add_to_cart_available:
            return IsolationPlan(
                strategy=CartStrategy.BLOCKED,
                foreign_lines=foreign,
                reason=(
                    "Amazon offered neither Buy Now nor Add to Cart for this "
                    "item, so it could not be ordered on its own."
                ),
            )

        if allow_set_aside:
            return IsolationPlan(
                strategy=CartStrategy.SET_ASIDE_OTHERS, foreign_lines=foreign
            )

        return IsolationPlan(
            strategy=CartStrategy.BLOCKED,
            foreign_lines=foreign,
            reason=(
                f"Your cart holds {len(foreign)} other item(s) and Buy Now was "
                "not available, so the order was stopped to avoid buying them "
                "by accident."
            ),
        )

    # ---- acting ----------------------------------------------------------

    def add_to_cart(self, reader: PageReader) -> None:
        """Click Add to Cart, then dismiss any add-on offer.

        Does not verify what ended up in the cart: that is the guard's job,
        against a freshly read cart.
        """
        locator, _ = reader.find(selectors.ADD_TO_CART_BUTTON, visible_only=True)
        if locator is None:
            raise AppError(
                ErrorCode.ADD_TO_CART_FAILED,
                context={"reason": "add_to_cart_button_missing"},
            )
        logger.info("Adding to cart")
        locator.click(timeout=10_000)
        reader.page.wait_for_load_state("domcontentloaded", timeout=20_000)
        self.decline_addons(reader)

    def select_one_time_purchase(self, reader: PageReader) -> bool:
        """Select the one-time purchase option when Subscribe & Save exists.

        Returns True when the one-time row is selected afterwards. The caller
        must treat False as a blocking condition: adding to the cart with the
        recurring row active would start a subscription.
        """
        if not reader.exists(selectors.SUBSCRIBE_AND_SAVE_ROW):
            return True

        locator, _ = reader.find(selectors.ONE_TIME_PURCHASE_RADIO, visible_only=True)
        if locator is None:
            locator, _ = reader.find(
                selectors.ONE_TIME_PURCHASE_ROW, visible_only=True
            )
        if locator is None:
            logger.warning("Subscribe & Save offered but no one-time option found")
            return False

        try:
            locator.click(timeout=8_000)
            reader.page.wait_for_timeout(750)
        except Exception:  # noqa: BLE001
            logger.warning("Could not select the one-time purchase option", exc_info=True)
            return False

        from app.automation.product_parser import PARSER

        still_preselected = PARSER.read_subscription_preselected(reader)
        if still_preselected:
            logger.warning("One-time purchase could not be confirmed as selected")
            return False
        logger.info("One-time purchase selected")
        return True

    def decline_addons(self, reader: PageReader) -> tuple[str, ...]:
        """Decline a protection plan or accessory offer if one appeared.

        Returns the names of any add-ons that were offered, for the activity
        log. Declining is always attempted; the guard still re-checks the
        checkout for extra line items afterwards, because an offer that could
        not be dismissed must block rather than be assumed harmless.
        """
        if not reader.exists(selectors.ADDON_SHEET_MARKERS, visible_only=True):
            return ()

        offered = reader.text(selectors.ADDON_SHEET_MARKERS).value or "an add-on"
        logger.info("Add-on offer appeared", extra={"offer": offered})

        locator, _ = reader.find(selectors.ADDON_DECLINE_BUTTON, visible_only=True)
        if locator is None:
            locator, _ = reader.find(selectors.ADDON_SHEET_CLOSE, visible_only=True)
        if locator is None:
            logger.warning("Could not find a way to decline the add-on offer")
            return (offered,)

        try:
            locator.click(timeout=8_000)
            reader.page.wait_for_load_state("domcontentloaded", timeout=15_000)
            logger.info("Add-on offer declined")
        except Exception:  # noqa: BLE001
            logger.warning("Could not decline the add-on offer", exc_info=True)
        return (offered,)

    def set_aside_foreign_items(
        self,
        reader: PageReader,
        *,
        target_asin: str,
        journal: IsolationJournal,
    ) -> CartState:
        """Move every unrelated cart line to Saved for later.

        The journal is updated before each move, so a crash leaves a record of
        what to restore. Row ids change on every render, so the cart is
        re-read between moves rather than caching the row handles.
        """
        wanted = target_asin.strip().upper()
        journal.strategy = CartStrategy.SET_ASIDE_OTHERS

        for attempt in range(25):
            cart = self.read_cart(reader)
            foreign = cart.foreign_lines(wanted)
            if not foreign:
                logger.info(
                    "Cart isolated", extra={"set_aside": len(journal.set_aside)}
                )
                return cart

            line = foreign[0]
            record = {
                "asin": line.asin,
                "title": line.title,
                "quantity": line.quantity,
            }
            if record not in journal.set_aside:
                journal.set_aside.append(record)

            if not self._click_row_action(
                reader, line, selectors.CART_ITEM_SAVE_FOR_LATER
            ):
                raise AppError(
                    ErrorCode.CART_CONFLICT,
                    context={
                        "reason": "could_not_set_aside",
                        "asin": line.asin,
                        "attempt": attempt,
                    },
                    detail_override=(
                        f'"{line.display_title}" could not be moved out of your '
                        "cart, so the order was stopped. Your cart was left as "
                        "it was."
                    ),
                )

        raise AppError(
            ErrorCode.CART_CONFLICT,
            context={"reason": "too_many_cart_items"},
            detail_override=(
                "Your cart holds too many other items to set aside safely, so "
                "the order was stopped."
            ),
        )

    def restore_set_aside_items(
        self, reader: PageReader, journal: IsolationJournal
    ) -> int:
        """Move previously set-aside items back into the cart.

        Best-effort and idempotent: it is called after a purchase completes,
        after one is blocked, and at startup if a journal was left pending.
        Failing to restore is logged and surfaced but never treated as a
        purchase failure, because the order outcome is already decided.
        """
        if not journal.has_pending_restore:
            return 0

        wanted = {
            str(item.get("asin", "")).strip().upper()
            for item in journal.set_aside
            if item.get("asin")
        }
        restored = 0
        for _ in range(len(wanted) + 5):
            container, _ = reader.find(selectors.SAVED_FOR_LATER_CONTAINER)
            if container is None:
                break
            moved_any = False
            try:
                rows = container.locator(selectors.SAVED_FOR_LATER_ITEMS)
                total = rows.count()
            except Exception:  # noqa: BLE001
                break
            for index in range(min(total, 60)):
                row = rows.nth(index)
                try:
                    asin = (row.get_attribute("data-asin", timeout=1_000) or "").upper()
                except Exception:  # noqa: BLE001
                    continue
                if asin not in wanted:
                    continue
                try:
                    control = row.locator(selectors.CART_ITEM_MOVE_TO_CART).first
                    if control.count() == 0:
                        continue
                    control.click(timeout=8_000)
                    reader.page.wait_for_load_state(
                        "domcontentloaded", timeout=15_000
                    )
                    restored += 1
                    wanted.discard(asin)
                    moved_any = True
                    break
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Could not move an item back to the cart",
                        extra={"asin": asin},
                        exc_info=True,
                    )
            if not moved_any or not wanted:
                break

        journal.restored = not wanted
        if wanted:
            logger.warning(
                "Some items could not be moved back to the cart",
                extra={"remaining": sorted(wanted)},
            )
        else:
            logger.info("Cart restored", extra={"restored": restored})
        return restored

    def _click_row_action(
        self, reader: PageReader, line: CartLine, selector: str
    ) -> bool:
        """Click a per-row control, locating the row by ASIN.

        Row ids are regenerated on every render, so the row is found by the
        stable ``data-asin`` attribute instead.
        """
        container, _ = reader.find(selectors.CART_ACTIVE_CONTAINER)
        if container is None:
            return False
        try:
            if line.asin:
                row = container.locator(selectors.cart_line_for(line.asin)).first
            else:
                row = container.locator(selectors.CART_LINE_ITEMS).first
            if row.count() == 0:
                return False
            control = row.locator(selector).first
            if control.count() == 0:
                return False
            control.click(timeout=8_000)
            reader.page.wait_for_load_state("domcontentloaded", timeout=15_000)
            return True
        except Exception:  # noqa: BLE001
            logger.warning(
                "A cart row action failed", extra={"asin": line.asin}, exc_info=True
            )
            return False

    def proceed_to_checkout(self, reader: PageReader) -> None:
        """Click "Proceed to checkout".

        The caller must have verified the cart contains only the target item;
        this control takes the entire active cart.
        """
        locator, _ = reader.find(
            selectors.CART_PROCEED_TO_CHECKOUT, visible_only=True
        )
        if locator is None:
            raise AppError(
                ErrorCode.UNEXPECTED_PAGE,
                context={"reason": "proceed_to_checkout_missing"},
                detail_override=(
                    "Amazon's checkout button could not be found, so nothing "
                    "was ordered."
                ),
            )
        logger.info("Proceeding to checkout")
        locator.click(timeout=15_000)
        reader.page.wait_for_load_state("domcontentloaded", timeout=30_000)

    def start_buy_now(self, reader: PageReader) -> None:
        """Click Buy Now, which creates a checkout that bypasses the cart."""
        locator, _ = reader.find(selectors.BUY_NOW_BUTTON, visible_only=True)
        if locator is None:
            raise AppError(
                ErrorCode.ADD_TO_CART_FAILED,
                context={"reason": "buy_now_missing"},
                detail_override=(
                    "Amazon's Buy Now button was no longer available, so "
                    "nothing was ordered."
                ),
            )
        logger.info("Starting Buy Now")
        locator.click(timeout=15_000)
        reader.page.wait_for_load_state("domcontentloaded", timeout=30_000)


MANAGER = CartManager()
