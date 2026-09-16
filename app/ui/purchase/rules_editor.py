"""Editing the rules a purchase must satisfy.

The editor's job is to make the *consequences* of each rule obvious, because
these are the settings that decide whether real money is spent. So:

* Two separate price limits, each with its own explanation, because "the item
  costs at most X" and "the order costs at most Y" are different promises and
  conflating them is how people overspend on shipping and tax.
* The conservative option is preselected everywhere.
* A combination that can never succeed -- an order limit below the item
  price -- is called out inline as soon as it is entered, rather than
  discovered later as a blocked purchase.
* Rarely-needed permissions (allow add-ons, allow a subscription, require
  Prime) are tucked behind an "Advanced" disclosure so the common path stays
  short, but they are never hidden from someone looking for them.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.money import Money
from app.purchasing.models import (
    ConditionPolicy,
    ProductSnapshot,
    PurchaseRules,
    SellerPolicy,
)
from app.ui.components import Card, MoneyField, QuantityField, StatusBadge
from app.ui.theme import StatusSeverity

logger = logging.getLogger("app.ui.purchase.rules")


class RulesEditor(QWidget):
    """A form for a :class:`PurchaseRules`."""

    #: Emitted whenever any field changes, with the current rules.
    rules_changed = Signal(object)
    #: Emitted with a warning string, or an empty string when there is none.
    warning_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rules = PurchaseRules(expected_asin="")
        self._snapshot: ProductSnapshot | None = None
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        layout.addWidget(self._build_quantity_and_price_card())
        layout.addWidget(self._build_offer_card())
        layout.addWidget(self._build_advanced_card())

        self._warning = StatusBadge()
        self._warning.setVisible(False)
        layout.addWidget(self._warning)

    # ---- construction ----------------------------------------------------

    def _build_quantity_and_price_card(self) -> Card:
        card = Card(
            title="How many, and how much",
            subtitle="The order is refused if either limit is exceeded.",
        )
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)

        self._quantity = QuantityField()
        self._quantity.setAccessibleName("Quantity")
        # The field may expose its change signal under either name depending
        # on how it wraps its spin box; connect whichever exists so the
        # warning line stays in step with the quantity.
        for signal_name in ("value_changed", "valueChanged"):
            signal = getattr(self._quantity, signal_name, None)
            if signal is not None and hasattr(signal, "connect"):
                signal.connect(self._on_change)
                break
        form.addRow("Quantity", self._quantity)

        self._max_item_price = MoneyField()
        self._max_item_price.setAccessibleName("Maximum item price")
        self._max_item_price.value_changed.connect(self._on_change)
        form.addRow("Maximum item price", self._max_item_price)
        form.addRow("", self._hint("The most you will pay for one unit."))

        self._max_order_total = MoneyField()
        self._max_order_total.setAccessibleName("Maximum complete order total")
        self._max_order_total.value_changed.connect(self._on_change)
        form.addRow("Maximum order total", self._max_order_total)
        form.addRow(
            "",
            self._hint(
                "The most the whole order may come to, including shipping, tax "
                "and any fees. Checked again just before ordering."
            ),
        )

        card.add_layout(form)
        return card

    def _build_offer_card(self) -> Card:
        card = Card(
            title="Which offer is acceptable",
            subtitle="A different seller or condition stops the purchase.",
        )
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)

        self._seller_policy = QComboBox()
        self._seller_policy.setAccessibleName("Seller policy")
        for policy in SellerPolicy:
            self._seller_policy.addItem(policy.label, policy)
        self._seller_policy.currentIndexChanged.connect(self._on_seller_policy_changed)
        form.addRow("Seller", self._seller_policy)

        self._seller_hint = self._hint(SellerPolicy.AMAZON_ONLY.description)
        form.addRow("", self._seller_hint)

        self._approved_sellers = QLineEdit()
        self._approved_sellers.setPlaceholderText("Seller names, separated by commas")
        self._approved_sellers.setAccessibleName("Approved sellers")
        self._approved_sellers.textChanged.connect(self._on_change)
        self._approved_row_label = QLabel("Approved sellers")
        form.addRow(self._approved_row_label, self._approved_sellers)
        self._approved_sellers.setVisible(False)
        self._approved_row_label.setVisible(False)

        self._condition_policy = QComboBox()
        self._condition_policy.setAccessibleName("Condition policy")
        for policy in ConditionPolicy:
            self._condition_policy.addItem(policy.label, policy)
        self._condition_policy.currentIndexChanged.connect(self._on_change)
        form.addRow("Condition", self._condition_policy)
        form.addRow(
            "",
            self._hint(
                "An item whose condition Amazon does not state is never bought."
            ),
        )

        card.add_layout(form)
        return card

    def _build_advanced_card(self) -> Card:
        card = Card(title="Advanced")
        group = QGroupBox()
        group.setFlat(True)
        inner = QVBoxLayout(group)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(8)

        self._require_address = QCheckBox(
            "Only order to the delivery address recorded now"
        )
        self._require_address.setChecked(True)
        self._require_address.stateChanged.connect(self._on_change)
        inner.addWidget(self._require_address)

        self._require_payment = QCheckBox(
            "Only order with the payment method recorded now"
        )
        self._require_payment.setChecked(True)
        self._require_payment.stateChanged.connect(self._on_change)
        inner.addWidget(self._require_payment)

        self._require_prime = QCheckBox("Only order Prime-eligible offers")
        self._require_prime.stateChanged.connect(self._on_change)
        self._require_prime.setToolTip(
            "If Amazon does not clearly show Prime eligibility, the order is "
            "refused rather than assumed."
        )
        inner.addWidget(self._require_prime)

        self._allow_addons = QCheckBox(
            "Allow Amazon to add extras such as a protection plan"
        )
        self._allow_addons.stateChanged.connect(self._on_change)
        inner.addWidget(self._allow_addons)

        self._allow_subscription = QCheckBox(
            "Allow this to be a repeating Subscribe & Save delivery"
        )
        self._allow_subscription.stateChanged.connect(self._on_change)
        inner.addWidget(self._allow_subscription)

        card.add_widget(group)
        return card

    @staticmethod
    def _hint(text: str) -> QLabel:
        label = QLabel(text)
        label.setProperty("role", "caption")
        label.setWordWrap(True)
        return label

    # ---- loading and reading --------------------------------------------

    def set_rules(
        self, rules: PurchaseRules, *, snapshot: ProductSnapshot | None = None
    ) -> None:
        """Load ``rules`` into the form without emitting change signals."""
        self._loading = True
        try:
            self._rules = rules
            self._snapshot = snapshot

            self._quantity.set_value(rules.quantity)
            if snapshot is not None:
                self._quantity.set_maximum_available(snapshot.max_quantity)
            self._max_item_price.set_value(rules.max_item_price)
            self._max_order_total.set_value(rules.max_order_total)

            self._select(self._seller_policy, rules.seller_policy)
            self._select(self._condition_policy, rules.condition_policy)
            self._approved_sellers.setText(", ".join(rules.approved_sellers))
            self._update_seller_visibility(rules.seller_policy)

            self._require_address.setChecked(rules.require_address_match)
            self._require_payment.setChecked(rules.require_payment_match)
            self._require_prime.setChecked(rules.require_prime)
            self._allow_addons.setChecked(rules.allow_addons)
            self._allow_subscription.setChecked(rules.allow_subscription)
        finally:
            self._loading = False
        self._on_change()

    @staticmethod
    def _select(combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def rules(self) -> PurchaseRules:
        """The rules as currently entered."""
        seller_policy = self._seller_policy.currentData() or SellerPolicy.AMAZON_ONLY
        condition_policy = (
            self._condition_policy.currentData() or ConditionPolicy.NEW_ONLY
        )
        approved = tuple(
            part.strip()
            for part in self._approved_sellers.text().split(",")
            if part.strip()
        )
        return replace(
            self._rules,
            quantity=self._quantity.value(),
            max_item_price=self._max_item_price.value(),
            max_order_total=self._max_order_total.value(),
            seller_policy=seller_policy,
            approved_sellers=approved,
            condition_policy=condition_policy,
            require_address_match=self._require_address.isChecked(),
            require_payment_match=self._require_payment.isChecked(),
            require_prime=self._require_prime.isChecked(),
            allow_addons=self._allow_addons.isChecked(),
            allow_subscription=self._allow_subscription.isChecked(),
        )

    def is_valid(self) -> bool:
        """Whether the form can produce usable rules."""
        return self._max_item_price.is_valid() and self._max_order_total.is_valid()

    # ---- reactions -------------------------------------------------------

    def _on_seller_policy_changed(self) -> None:
        policy = self._seller_policy.currentData() or SellerPolicy.AMAZON_ONLY
        self._seller_hint.setText(policy.description)
        self._update_seller_visibility(policy)
        self._on_change()

    def _update_seller_visibility(self, policy: SellerPolicy) -> None:
        needed = policy is SellerPolicy.APPROVED_LIST
        self._approved_sellers.setVisible(needed)
        self._approved_row_label.setVisible(needed)

    def _on_change(self) -> None:
        if self._loading:
            return
        rules = self.rules()
        warning = self._warning_for(rules)
        if warning:
            self._warning.set_status(warning, StatusSeverity.WARNING)
            self._warning.setVisible(True)
        else:
            self._warning.setVisible(False)
        self.warning_changed.emit(warning)
        self.rules_changed.emit(rules)

    def _warning_for(self, rules: PurchaseRules) -> str:
        """Catch rule combinations that could never succeed."""
        if not self.is_valid():
            return "Check the price limits: one of them is not a valid amount."

        item = rules.max_item_price
        total = rules.max_order_total
        if item is not None and total is not None:
            minimum_total = item * rules.quantity
            if total.currency == item.currency and total < minimum_total:
                return (
                    f"Your order limit of {total.format()} is below "
                    f"{minimum_total.format()} for {rules.quantity} at "
                    f"{item.format()}, so no order could ever go through."
                )

        if (
            rules.seller_policy is SellerPolicy.APPROVED_LIST
            and not rules.approved_sellers
        ):
            return "Add at least one seller name, or choose a different seller rule."

        if (
            rules.seller_policy is SellerPolicy.AMAZON_OR_MANUFACTURER
            and not rules.brand
        ):
            return (
                "The brand could not be read from the product page, so only "
                "Amazon itself will be accepted."
            )

        snapshot = self._snapshot
        if snapshot is not None and snapshot.price is not None and item is not None:
            if item.currency == snapshot.price.currency and item < snapshot.price:
                return (
                    f"The current price is {snapshot.price.format()}, above your "
                    f"limit of {item.format()}. Nothing will be bought until the "
                    "price falls."
                )

        if rules.allow_subscription:
            return "This may start a repeating delivery rather than a one-off order."

        return ""

    def current_warning(self) -> str:
        return self._warning_for(self.rules())
