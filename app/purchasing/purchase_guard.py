"""The Purchase Guard: the deterministic gate every purchase must pass.

This module contains no browser code, no I/O and no randomness. It is a pure
function from (rules, observed state) to a :class:`GuardReport`, which makes
it exhaustively testable -- and it is the *only* thing permitted to authorise
a purchase. Assisted mode and automatic mode call exactly the same function
with the same rules; there is no weaker path.

Design rules, each of which exists because violating it could cost the user
money:

* **Absent data fails.** If a required value could not be read, the matching
  check fails. It never defaults to "probably fine".
* **No warn-and-continue.** A required check that fails blocks. There is no
  severity level that lets a purchase through with a caveat.
* **The final total is re-checked last.** Shipping, tax and fees only exist
  at checkout, so the order-total rule is enforced against the real total
  moments before submission.
* **Nothing is substituted.** A different ASIN, variation, seller or
  condition blocks rather than adapting.
"""

from __future__ import annotations

from app.core.errors import ErrorCode
from app.core.money import Money
from app.purchasing.models import (
    CartState,
    CheckoutSnapshot,
    ItemCondition,
    ProductSnapshot,
    PurchaseRules,
    normalise_label,
)
from app.purchasing.validation import (
    CheckStatus,
    GuardCheck,
    GuardPhase,
    GuardReport,
    check_fail,
    check_not_applicable,
    check_pass,
    check_skipped,
)

# Check identifiers. Stable strings: they are persisted and referenced by the
# UI, so they are never renamed.
CHECK_ASIN = "asin"
CHECK_VARIATION = "variation"
CHECK_AVAILABILITY = "availability"
CHECK_CONDITION = "condition"
CHECK_SELLER = "seller"
CHECK_SELLER_CHANGED = "seller_changed"
CHECK_SHIPS_FROM = "ships_from"
CHECK_ITEM_PRICE = "item_price"
CHECK_MAX_ITEM_PRICE = "max_item_price"
CHECK_QUANTITY = "quantity"
CHECK_PRIME = "prime"
CHECK_SUBSCRIPTION = "subscription"
CHECK_CART_CONTENTS = "cart_contents"
CHECK_CART_QUANTITY = "cart_quantity"
CHECK_ADDONS = "addons"
CHECK_ORDER_TOTAL = "order_total"
CHECK_MAX_ORDER_TOTAL = "max_order_total"
CHECK_ADDRESS = "address"
CHECK_PAYMENT = "payment"
CHECK_PLACE_ORDER_CONTROL = "place_order_control"

#: Display order for the PASS/FAIL table, matching the order a user would
#: think about them: what, how many, from whom, for how much, to where.
CHECK_ORDER: tuple[str, ...] = (
    CHECK_ASIN,
    CHECK_VARIATION,
    CHECK_AVAILABILITY,
    CHECK_QUANTITY,
    CHECK_CONDITION,
    CHECK_SELLER,
    CHECK_SELLER_CHANGED,
    CHECK_SHIPS_FROM,
    CHECK_PRIME,
    CHECK_SUBSCRIPTION,
    CHECK_ITEM_PRICE,
    CHECK_MAX_ITEM_PRICE,
    CHECK_CART_CONTENTS,
    CHECK_CART_QUANTITY,
    CHECK_ADDONS,
    CHECK_ORDER_TOTAL,
    CHECK_MAX_ORDER_TOTAL,
    CHECK_ADDRESS,
    CHECK_PAYMENT,
    CHECK_PLACE_ORDER_CONTROL,
)


def _money(value: Money | None) -> str:
    return value.format() if value is not None else "not shown"


def _sorted(checks: list[GuardCheck]) -> tuple[GuardCheck, ...]:
    position = {check_id: index for index, check_id in enumerate(CHECK_ORDER)}
    return tuple(sorted(checks, key=lambda check: position.get(check.check_id, 999)))


class PurchaseGuard:
    """Validates observed Amazon state against the user's rules."""

    # -- phase 1: the product page ----------------------------------------

    def check_product(
        self, rules: PurchaseRules, product: ProductSnapshot
    ) -> GuardReport:
        """Validate what the product page showed, before touching the cart."""
        checks: list[GuardCheck] = [
            self._check_asin(rules, product),
            self._check_variation(rules, product),
            self._check_availability(product),
            self._check_condition(rules, product),
            self._check_seller(rules, product),
            self._check_seller_changed(rules, product),
            self._check_ships_from(rules, product),
            self._check_quantity(rules, product),
            self._check_prime(rules, product),
            self._check_subscription(rules, product),
            self._check_item_price_known(product),
            self._check_max_item_price(rules, product),
        ]
        return GuardReport(phase=GuardPhase.PRE_CART, checks=_sorted(checks))

    # -- phase 2: the cart -------------------------------------------------

    def check_cart(
        self, rules: PurchaseRules, product: ProductSnapshot, cart: CartState
    ) -> GuardReport:
        """Re-validate the product and confirm the cart holds only the target.

        Called after the item has been put into a checkout but before
        proceeding, so an unrelated item can still be caught without any risk
        of it being ordered.
        """
        checks: list[GuardCheck] = list(self.check_product(rules, product).checks)
        checks.append(self._check_cart_contents(rules, cart))
        checks.append(self._check_cart_quantity(rules, cart))
        return GuardReport(phase=GuardPhase.PRE_CHECKOUT, checks=_sorted(checks))

    # -- phase 3: the final review ----------------------------------------

    def check_final(
        self,
        rules: PurchaseRules,
        product: ProductSnapshot,
        checkout: CheckoutSnapshot,
    ) -> GuardReport:
        """The last gate before an order may be submitted.

        Everything from the earlier phases is re-run against the checkout's
        own view of the order, plus the checks that only exist here: the real
        order total, the delivery address, the payment method and unexpected
        add-ons.
        """
        checks: list[GuardCheck] = [
            self._check_asin_in_checkout(rules, checkout),
            self._check_variation(rules, product),
            self._check_condition(rules, product),
            self._check_seller(rules, product),
            self._check_seller_changed(rules, product),
            self._check_quantity_in_checkout(rules, checkout),
            self._check_subscription_in_checkout(rules, checkout),
            self._check_item_price_in_checkout(rules, checkout),
            self._check_addons(rules, checkout),
            self._check_order_total_known(checkout),
            self._check_max_order_total(rules, checkout),
            self._check_address(rules, checkout),
            self._check_payment(rules, checkout),
            self._check_place_order_control(checkout),
        ]
        return GuardReport(phase=GuardPhase.PRE_SUBMIT, checks=_sorted(checks))

    # -- individual checks -------------------------------------------------

    def _check_asin(self, rules: PurchaseRules, product: ProductSnapshot) -> GuardCheck:
        expected = rules.expected_asin.strip().upper()
        actual = (product.asin or "").strip().upper()
        if not actual:
            return check_fail(
                CHECK_ASIN,
                "Product",
                ErrorCode.ASIN_MISMATCH,
                expected=expected,
                actual="not found",
                detail="The page did not identify which item it was showing.",
            )
        if actual != expected:
            return check_fail(
                CHECK_ASIN,
                "Product",
                ErrorCode.ASIN_MISMATCH,
                expected=expected,
                actual=actual,
            )
        return check_pass(CHECK_ASIN, "Product", actual=actual)

    def _check_asin_in_checkout(
        self, rules: PurchaseRules, checkout: CheckoutSnapshot
    ) -> GuardCheck:
        expected = rules.expected_asin.strip().upper()
        matching = checkout.lines_for(expected)
        if not checkout.lines:
            return check_fail(
                CHECK_ASIN,
                "Product",
                ErrorCode.CHECKOUT_CHANGED,
                expected=expected,
                actual="no items could be read",
                detail="The checkout page did not list any items to verify.",
            )
        if not matching:
            return check_fail(
                CHECK_ASIN,
                "Product",
                ErrorCode.ASIN_MISMATCH,
                expected=expected,
                actual=", ".join(line.asin or "unknown" for line in checkout.lines),
            )
        return check_pass(CHECK_ASIN, "Product", actual=expected)

    def _check_variation(
        self, rules: PurchaseRules, product: ProductSnapshot
    ) -> GuardCheck:
        expected = rules.expected_variation
        actual = product.variation
        if expected.is_empty and actual.is_empty:
            return check_not_applicable(CHECK_VARIATION, "Version")
        if expected.is_empty:
            # No expectation was recorded, so there is nothing to compare
            # against. Reported, not treated as a pass.
            return check_skipped(
                CHECK_VARIATION,
                "Version",
                detail="No specific version was recorded for this item.",
            )
        if not expected.matches(actual):
            return check_fail(
                CHECK_VARIATION,
                "Version",
                ErrorCode.VARIATION_CHANGED,
                expected=expected.describe_full(),
                actual=actual.describe_full() if not actual.is_empty else "not shown",
            )
        return check_pass(CHECK_VARIATION, "Version", actual=actual.describe_full())

    def _check_availability(self, product: ProductSnapshot) -> GuardCheck:
        if product.availability.is_buyable:
            return check_pass(
                CHECK_AVAILABILITY, "Availability", actual=product.availability.label
            )
        return check_fail(
            CHECK_AVAILABILITY,
            "Availability",
            ErrorCode.PRODUCT_UNAVAILABLE,
            expected="In stock",
            actual=product.availability_text or product.availability.label,
        )

    def _check_condition(
        self, rules: PurchaseRules, product: ProductSnapshot
    ) -> GuardCheck:
        condition = product.condition
        if not rules.condition_allowed(condition):
            return check_fail(
                CHECK_CONDITION,
                "Condition",
                ErrorCode.CONDITION_NOT_ALLOWED,
                expected=rules.condition_policy.label,
                actual=condition.label,
                detail=(
                    "Amazon did not state the condition, so it could not be "
                    "confirmed as acceptable."
                    if condition is ItemCondition.UNKNOWN
                    else None
                ),
            )
        return check_pass(CHECK_CONDITION, "Condition", actual=condition.label)

    def _check_seller(
        self, rules: PurchaseRules, product: ProductSnapshot
    ) -> GuardCheck:
        if not rules.seller_allowed(product.seller):
            return check_fail(
                CHECK_SELLER,
                "Seller",
                ErrorCode.SELLER_NOT_ALLOWED,
                expected=rules.describe_seller_rule(),
                actual=product.seller or "not shown",
                detail=(
                    "Amazon did not say who the seller was."
                    if not product.seller
                    else None
                ),
            )
        return check_pass(
            CHECK_SELLER, "Seller", actual=product.seller or "Amazon.com"
        )

    def _check_seller_changed(
        self, rules: PurchaseRules, product: ProductSnapshot
    ) -> GuardCheck:
        if not rules.expected_seller:
            return check_skipped(
                CHECK_SELLER_CHANGED,
                "Same seller as before",
                detail="No previous seller was recorded.",
            )
        expected = normalise_label(rules.expected_seller)
        actual = normalise_label(product.seller)
        if not actual:
            return check_fail(
                CHECK_SELLER_CHANGED,
                "Same seller as before",
                ErrorCode.SELLER_CHANGED,
                expected=rules.expected_seller,
                actual="not shown",
            )
        if expected != actual:
            return check_fail(
                CHECK_SELLER_CHANGED,
                "Same seller as before",
                ErrorCode.SELLER_CHANGED,
                expected=rules.expected_seller,
                actual=product.seller,
            )
        return check_pass(
            CHECK_SELLER_CHANGED, "Same seller as before", actual=product.seller
        )

    def _check_ships_from(
        self, rules: PurchaseRules, product: ProductSnapshot
    ) -> GuardCheck:
        if not rules.expected_ships_from:
            return check_skipped(
                CHECK_SHIPS_FROM, "Ships from", detail="Not recorded."
            )
        expected = normalise_label(rules.expected_ships_from)
        actual = normalise_label(product.ships_from)
        if expected != actual:
            return check_fail(
                CHECK_SHIPS_FROM,
                "Ships from",
                ErrorCode.SELLER_CHANGED,
                expected=rules.expected_ships_from,
                actual=product.ships_from or "not shown",
            )
        return check_pass(CHECK_SHIPS_FROM, "Ships from", actual=product.ships_from)

    def _check_quantity(
        self, rules: PurchaseRules, product: ProductSnapshot
    ) -> GuardCheck:
        wanted = rules.quantity
        if wanted < 1:
            return check_fail(
                CHECK_QUANTITY,
                "Quantity",
                ErrorCode.QUANTITY_UNAVAILABLE,
                expected=str(wanted),
                actual="invalid",
            )
        if product.max_quantity is not None and wanted > product.max_quantity:
            return check_fail(
                CHECK_QUANTITY,
                "Quantity",
                ErrorCode.QUANTITY_UNAVAILABLE,
                expected=str(wanted),
                actual=f"at most {product.max_quantity} available",
            )
        return check_pass(CHECK_QUANTITY, "Quantity", actual=str(wanted))

    def _check_quantity_in_checkout(
        self, rules: PurchaseRules, checkout: CheckoutSnapshot
    ) -> GuardCheck:
        expected = rules.quantity
        matching = checkout.lines_for(rules.expected_asin)
        if not matching:
            # The ASIN check already reports this; avoid a duplicate failure.
            return check_not_applicable(CHECK_QUANTITY, "Quantity")
        actual = sum(line.quantity for line in matching)
        if actual != expected:
            return check_fail(
                CHECK_QUANTITY,
                "Quantity",
                ErrorCode.QUANTITY_UNAVAILABLE,
                expected=str(expected),
                actual=str(actual),
            )
        return check_pass(CHECK_QUANTITY, "Quantity", actual=str(actual))

    def _check_prime(
        self, rules: PurchaseRules, product: ProductSnapshot
    ) -> GuardCheck:
        if not rules.require_prime:
            return check_not_applicable(CHECK_PRIME, "Prime delivery")
        if product.prime_eligible is None:
            # Required but undetectable: this must fail, not skip.
            return check_fail(
                CHECK_PRIME,
                "Prime delivery",
                ErrorCode.UNEXPECTED_PAGE,
                expected="Prime eligible",
                actual="could not be determined",
                detail="Amazon did not clearly show Prime eligibility.",
            )
        if not product.prime_eligible:
            return check_fail(
                CHECK_PRIME,
                "Prime delivery",
                ErrorCode.UNEXPECTED_PAGE,
                expected="Prime eligible",
                actual="not Prime eligible",
            )
        return check_pass(CHECK_PRIME, "Prime delivery", actual="Prime eligible")

    def _check_subscription(
        self, rules: PurchaseRules, product: ProductSnapshot
    ) -> GuardCheck:
        if rules.allow_subscription:
            return check_not_applicable(CHECK_SUBSCRIPTION, "One-time purchase")
        if product.subscription_preselected:
            return check_fail(
                CHECK_SUBSCRIPTION,
                "One-time purchase",
                ErrorCode.SUBSCRIPTION_DETECTED,
                expected="One-time purchase",
                actual="Subscribe & Save was pre-selected",
            )
        return check_pass(
            CHECK_SUBSCRIPTION, "One-time purchase", actual="One-time purchase"
        )

    def _check_subscription_in_checkout(
        self, rules: PurchaseRules, checkout: CheckoutSnapshot
    ) -> GuardCheck:
        if rules.allow_subscription:
            return check_not_applicable(CHECK_SUBSCRIPTION, "One-time purchase")
        if checkout.is_subscription:
            return check_fail(
                CHECK_SUBSCRIPTION,
                "One-time purchase",
                ErrorCode.SUBSCRIPTION_DETECTED,
                expected="One-time purchase",
                actual="recurring delivery",
            )
        return check_pass(
            CHECK_SUBSCRIPTION, "One-time purchase", actual="One-time purchase"
        )

    def _check_item_price_known(self, product: ProductSnapshot) -> GuardCheck:
        if product.price is None:
            return check_fail(
                CHECK_ITEM_PRICE,
                "Item price",
                ErrorCode.PRICE_UNAVAILABLE,
                expected="a readable price",
                actual="not shown",
            )
        return check_pass(CHECK_ITEM_PRICE, "Item price", actual=product.price.format())

    def _check_max_item_price(
        self, rules: PurchaseRules, product: ProductSnapshot
    ) -> GuardCheck:
        limit = rules.max_item_price
        if limit is None:
            return check_skipped(
                CHECK_MAX_ITEM_PRICE,
                "Maximum item price",
                detail="No maximum item price was set.",
            )
        if product.price is None:
            return check_fail(
                CHECK_MAX_ITEM_PRICE,
                "Maximum item price",
                ErrorCode.PRICE_UNAVAILABLE,
                expected=f"at most {limit.format()}",
                actual="price not shown",
            )
        if product.price.currency != limit.currency:
            return check_fail(
                CHECK_MAX_ITEM_PRICE,
                "Maximum item price",
                ErrorCode.PRICE_UNAVAILABLE,
                expected=f"at most {limit.format()}",
                actual=f"{product.price.format()} ({product.price.currency})",
                detail="The price was shown in a different currency.",
            )
        if product.price > limit:
            return check_fail(
                CHECK_MAX_ITEM_PRICE,
                "Maximum item price",
                ErrorCode.PRICE_ABOVE_LIMIT,
                expected=f"at most {limit.format()}",
                actual=product.price.format(),
            )
        return check_pass(
            CHECK_MAX_ITEM_PRICE,
            "Maximum item price",
            expected=f"at most {limit.format()}",
            actual=product.price.format(),
        )

    def _check_item_price_in_checkout(
        self, rules: PurchaseRules, checkout: CheckoutSnapshot
    ) -> GuardCheck:
        limit = rules.max_item_price
        matching = checkout.lines_for(rules.expected_asin)
        unit_prices = [
            line.unit_price for line in matching if line.unit_price is not None
        ]
        if not unit_prices:
            if limit is None:
                return check_skipped(
                    CHECK_MAX_ITEM_PRICE,
                    "Maximum item price",
                    detail="No maximum item price was set.",
                )
            # The order-total check still protects the user here, but a
            # configured item-price rule must be verifiable.
            return check_fail(
                CHECK_MAX_ITEM_PRICE,
                "Maximum item price",
                ErrorCode.CHECKOUT_CHANGED,
                expected=f"at most {limit.format()}",
                actual="the item price was not shown at checkout",
            )
        highest = max(unit_prices, key=lambda money: money.cents)
        if limit is None:
            return check_skipped(
                CHECK_MAX_ITEM_PRICE,
                "Maximum item price",
                detail="No maximum item price was set.",
            )
        if highest.currency != limit.currency or highest > limit:
            return check_fail(
                CHECK_MAX_ITEM_PRICE,
                "Maximum item price",
                ErrorCode.PRICE_ABOVE_LIMIT,
                expected=f"at most {limit.format()}",
                actual=highest.format(),
            )
        return check_pass(
            CHECK_MAX_ITEM_PRICE,
            "Maximum item price",
            expected=f"at most {limit.format()}",
            actual=highest.format(),
        )

    def _check_cart_contents(
        self, rules: PurchaseRules, cart: CartState
    ) -> GuardCheck:
        foreign = cart.foreign_lines(rules.expected_asin)
        if foreign:
            names = ", ".join(line.display_title for line in foreign[:3])
            if len(foreign) > 3:
                names = f"{names} and {len(foreign) - 3} more"
            return check_fail(
                CHECK_CART_CONTENTS,
                "Only your item in the order",
                ErrorCode.UNEXPECTED_CART_ITEMS,
                expected="only the item you chose",
                actual=names,
            )
        if not cart.lines_for(rules.expected_asin):
            return check_fail(
                CHECK_CART_CONTENTS,
                "Only your item in the order",
                ErrorCode.ADD_TO_CART_FAILED,
                expected="the item you chose",
                actual="the order was empty",
            )
        return check_pass(
            CHECK_CART_CONTENTS, "Only your item in the order", actual="1 item"
        )

    def _check_cart_quantity(
        self, rules: PurchaseRules, cart: CartState
    ) -> GuardCheck:
        matching = cart.lines_for(rules.expected_asin)
        if not matching:
            return check_not_applicable(CHECK_CART_QUANTITY, "Quantity in the order")
        actual = sum(line.quantity for line in matching)
        if actual != rules.quantity:
            return check_fail(
                CHECK_CART_QUANTITY,
                "Quantity in the order",
                ErrorCode.QUANTITY_UNAVAILABLE,
                expected=str(rules.quantity),
                actual=str(actual),
            )
        return check_pass(
            CHECK_CART_QUANTITY, "Quantity in the order", actual=str(actual)
        )

    def _check_addons(
        self, rules: PurchaseRules, checkout: CheckoutSnapshot
    ) -> GuardCheck:
        foreign = checkout.foreign_lines(rules.expected_asin)
        extras = list(checkout.addons) + [line.display_title for line in foreign]
        if not extras:
            return check_pass(
                CHECK_ADDONS, "No extra items added", actual="none"
            )
        if rules.allow_addons:
            return check_pass(
                CHECK_ADDONS, "No extra items added", actual=", ".join(extras)
            )
        return check_fail(
            CHECK_ADDONS,
            "No extra items added",
            ErrorCode.UNEXPECTED_ADDONS
            if checkout.addons
            else ErrorCode.UNEXPECTED_CART_ITEMS,
            expected="no extras",
            actual=", ".join(extras[:3]),
        )

    def _check_order_total_known(self, checkout: CheckoutSnapshot) -> GuardCheck:
        if checkout.order_total is None:
            return check_fail(
                CHECK_ORDER_TOTAL,
                "Order total",
                ErrorCode.CHECKOUT_CHANGED,
                expected="a readable order total",
                actual="not shown",
                detail="Amazon's order total could not be read, so it could "
                "not be checked against your limit.",
            )
        return check_pass(
            CHECK_ORDER_TOTAL, "Order total", actual=checkout.order_total.format()
        )

    def _check_max_order_total(
        self, rules: PurchaseRules, checkout: CheckoutSnapshot
    ) -> GuardCheck:
        limit = rules.max_order_total
        if limit is None:
            return check_skipped(
                CHECK_MAX_ORDER_TOTAL,
                "Maximum order total",
                detail="No maximum order total was set.",
            )
        total = checkout.order_total
        if total is None:
            return check_fail(
                CHECK_MAX_ORDER_TOTAL,
                "Maximum order total",
                ErrorCode.CHECKOUT_CHANGED,
                expected=f"at most {limit.format()}",
                actual="the total was not shown",
            )
        if total.currency != limit.currency:
            return check_fail(
                CHECK_MAX_ORDER_TOTAL,
                "Maximum order total",
                ErrorCode.CHECKOUT_CHANGED,
                expected=f"at most {limit.format()}",
                actual=f"{total.format()} ({total.currency})",
                detail="The total was shown in a different currency.",
            )
        if total > limit:
            return check_fail(
                CHECK_MAX_ORDER_TOTAL,
                "Maximum order total",
                ErrorCode.TOTAL_ABOVE_LIMIT,
                expected=f"at most {limit.format()}",
                actual=total.format(),
            )
        return check_pass(
            CHECK_MAX_ORDER_TOTAL,
            "Maximum order total",
            expected=f"at most {limit.format()}",
            actual=total.format(),
        )

    def _check_address(
        self, rules: PurchaseRules, checkout: CheckoutSnapshot
    ) -> GuardCheck:
        actual = (checkout.address_label or "").strip()
        if not actual:
            return check_fail(
                CHECK_ADDRESS,
                "Delivery address",
                ErrorCode.ADDRESS_PROBLEM,
                expected=rules.expected_address_label or "a delivery address",
                actual="none shown",
            )
        if not rules.require_address_match or not rules.expected_address_label:
            return check_pass(CHECK_ADDRESS, "Delivery address", actual=actual)
        if normalise_label(actual) != normalise_label(rules.expected_address_label):
            return check_fail(
                CHECK_ADDRESS,
                "Delivery address",
                ErrorCode.ADDRESS_PROBLEM,
                expected=rules.expected_address_label,
                actual=actual,
            )
        return check_pass(CHECK_ADDRESS, "Delivery address", actual=actual)

    def _check_payment(
        self, rules: PurchaseRules, checkout: CheckoutSnapshot
    ) -> GuardCheck:
        actual = (checkout.payment_label or "").strip()
        if not actual:
            return check_fail(
                CHECK_PAYMENT,
                "Payment method",
                ErrorCode.PAYMENT_METHOD_PROBLEM,
                expected=rules.expected_payment_label or "a payment method",
                actual="none shown",
            )
        if not rules.require_payment_match or not rules.expected_payment_label:
            return check_pass(CHECK_PAYMENT, "Payment method", actual=actual)
        if normalise_label(actual) != normalise_label(rules.expected_payment_label):
            return check_fail(
                CHECK_PAYMENT,
                "Payment method",
                ErrorCode.PAYMENT_METHOD_PROBLEM,
                expected=rules.expected_payment_label,
                actual=actual,
            )
        return check_pass(CHECK_PAYMENT, "Payment method", actual=actual)

    def _check_place_order_control(self, checkout: CheckoutSnapshot) -> GuardCheck:
        if not checkout.place_order_control_found:
            return check_fail(
                CHECK_PLACE_ORDER_CONTROL,
                "Order button located",
                ErrorCode.CHECKOUT_CHANGED,
                expected="found",
                actual="not found",
                detail="Amazon's own order button could not be found on the page.",
            )
        return check_pass(
            CHECK_PLACE_ORDER_CONTROL, "Order button located", actual="found"
        )


#: Module-level instance. The guard is stateless, so one is enough.
GUARD = PurchaseGuard()


def verify_total_unchanged(
    approved: Money | None, current: Money | None
) -> GuardCheck:
    """Confirm the order total has not moved since the user approved it.

    Run immediately before submission in assisted mode. A total that changed
    while the confirmation dialog was open must invalidate the approval --
    otherwise the user's consent applies to a different amount than the one
    they will be charged.
    """
    title = "Total unchanged since you approved it"
    if approved is None or current is None:
        return check_fail(
            CHECK_ORDER_TOTAL,
            title,
            ErrorCode.CHECKOUT_TOTAL_CHANGED,
            expected=_money(approved),
            actual=_money(current),
            detail="The total could not be compared, so the order was stopped.",
        )
    if approved.currency != current.currency or approved.cents != current.cents:
        return check_fail(
            CHECK_ORDER_TOTAL,
            title,
            ErrorCode.CHECKOUT_TOTAL_CHANGED,
            expected=approved.format(),
            actual=current.format(),
        )
    return check_pass(CHECK_ORDER_TOTAL, title, actual=current.format())
