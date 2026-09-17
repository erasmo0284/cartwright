"""The final confirmation before an order is placed.

This is the most consequential screen in the application, so it is built
around one idea: **the user must be able to see exactly what they are about
to buy and for how much, and must not be able to approve it by accident.**

Concretely:

* Every figure shown is the one read from Amazon's own checkout moments ago,
  not a figure the app calculated.
* The order button carries the exact total in its label, so the amount is
  part of the decision rather than something above it.
* Cancel is the default button and the Escape route. Enter cannot place an
  order.
* The button is disabled outright unless the Purchase Guard passed and a
  total could be read.
* The full PASS/FAIL table is on screen, not hidden behind a disclosure.
* The dialog opens at the height its content needs, so the delivery address
  and the payment method are not below the fold. See :meth:`sizeHint`.
"""

from __future__ import annotations

import logging
from typing import Final

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.branding import BRAND
from app.core.money import Money
from app.purchasing.models import CartStrategy
from app.purchasing.purchase_service import PurchaseReview
from app.ui.components import (
    ButtonRow,
    Card,
    GuardTable,
    PrimaryButton,
    ProductSummaryCard,
    StatusBadge,
    SubtleButton,
)
from app.ui.theme import StatusSeverity, font_body, font_metric, font_title

logger = logging.getLogger("app.ui.dialogs.confirm")

#: The most of the screen's usable height this dialog will take before it
#: falls back to scrolling. Nine tenths keeps the window frame and a little of
#: the desktop in view, so it still reads as a dialog over the application
#: rather than as the whole display.
_MAX_SCREEN_FRACTION: Final[float] = 0.9

#: The smallest useful size. The height is deliberately well under the content
#: height: on a small screen, or at 150% scaling, the dialog has to be allowed
#: to shrink and scroll instead of growing past the bottom of the screen.
_MIN_WIDTH: Final[int] = 560
_MIN_HEIGHT: Final[int] = 460

#: Qt's own maximum widget extent, used when there is no screen to measure.
_MAX_DIALOG_HEIGHT: Final[int] = 16_777_215


class ConfirmPurchaseDialog(QDialog):
    """Shows the prepared order and asks for one deliberate confirmation."""

    def __init__(self, review: PurchaseReview, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._review = review

        self.setWindowTitle(f"Ready to purchase - {BRAND.display_name}")
        self.setModal(True)
        self.setMinimumSize(_MIN_WIDTH, _MIN_HEIGHT)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        heading = QLabel("Ready to purchase")
        heading.setFont(font_title())
        heading.setProperty("role", "title")
        heading_wrapper = QWidget()
        heading_layout = QVBoxLayout(heading_wrapper)
        heading_layout.setContentsMargins(20, 18, 20, 8)
        heading_layout.addWidget(heading)
        subtitle = QLabel(
            "Nothing has been ordered yet. Check the details, then place the "
            "order."
        )
        subtitle.setProperty("role", "subtitle")
        subtitle.setWordWrap(True)
        heading_layout.addWidget(subtitle)
        outer.addWidget(heading_wrapper)
        self._heading_wrapper = heading_wrapper

        scroll = QScrollArea()
        scroll.setObjectName("ScrollAreaFlat")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(20, 8, 20, 16)
        body_layout.setSpacing(12)

        product_card = ProductSummaryCard(compact=True)
        product_card.set_product(review.snapshot)
        body_layout.addWidget(product_card)

        body_layout.addWidget(self._build_order_card())
        body_layout.addWidget(self._build_delivery_card())
        body_layout.addWidget(self._build_guard_card())
        body_layout.addStretch(1)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self._scroll = scroll
        self._body = body

        footer = self._build_footer()
        outer.addWidget(footer)
        self._footer = footer

        # Open at the size the content asks for. Qt would otherwise use the
        # minimum, which put the delivery card below the fold.
        self.resize(self.sizeHint())

        logger.info(
            "Showing the purchase confirmation",
            extra={
                "purchase_job_id": review.purchase_job_id,
                "total_cents": review.order_total.cents if review.order_total else None,
                "guard_passed": review.report.passed,
                "strategy": review.strategy.value,
            },
        )

    # ---- sizing ----------------------------------------------------------

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        """The size at which nothing essential is hidden.

        A :class:`QScrollArea` reports a small, fixed size hint of its own
        rather than its content's, so a dialog built around one opens at its
        minimum height and the last card ends up below the fold. On this
        screen that card is "Where it goes and how it is paid" -- the delivery
        address and the payment method -- which is precisely what a person has
        to check before real money is spent.

        The height asked for is therefore the content's own, capped at
        :data:`_MAX_SCREEN_FRACTION` of the usable screen height. The scroll
        area is kept for the case where the content genuinely does not fit: a
        small screen, or a display at 150% scaling.
        """
        hint = super().sizeHint()
        if not hasattr(self, "_footer"):
            # Qt may ask while the dialog is still being built, before there
            # is any content to measure.
            return hint
        width = max(hint.width(), self.minimumWidth())
        return QSize(width, min(self._content_height(width), self._height_budget()))

    def _content_height(self, width: int) -> int:
        """The height that puts every card on screen at ``width``."""
        layout = self._body.layout()
        # Word-wrapped labels only know their height once they know their
        # width, and the width they will get is the viewport's. It is measured
        # as though a scrollbar were present, which is the narrower case: a
        # line that wraps onto two is then allowed for rather than clipped.
        frame = 2 * self._scroll.frameWidth()
        bar = self._scroll.verticalScrollBar().sizeHint().width()
        viewport = max(width - frame - bar, 1)
        if layout is not None and layout.hasHeightForWidth():
            content = layout.heightForWidth(viewport)
        else:
            content = self._body.sizeHint().height()
        chrome = (
            self._heading_wrapper.heightForWidth(width)
            if self._heading_wrapper.hasHeightForWidth()
            else self._heading_wrapper.sizeHint().height()
        ) + self._footer.sizeHint().height()
        return chrome + content + frame

    def _height_budget(self) -> int:
        """The tallest this dialog may open, given the screen it is on."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            # No screen to measure (offscreen rendering, or a test): the
            # content's own height is then the only sensible answer.
            return _MAX_DIALOG_HEIGHT
        return int(screen.availableGeometry().height() * _MAX_SCREEN_FRACTION)

    # ---- sections --------------------------------------------------------

    def _build_order_card(self) -> Card:
        checkout = self._review.checkout
        card = Card(title="What you will be charged")
        grid = QGridLayout()
        grid.setColumnStretch(0, 1)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)

        row = 0
        quantity = sum(
            line.units for line in checkout.lines_for(self._review.rules.expected_asin)
        ) or self._review.rules.quantity
        for label, value in (
            ("Quantity", str(quantity)),
            ("Seller", self._review.snapshot.seller or "Not shown"),
            ("Condition", self._review.snapshot.condition.label),
        ):
            grid.addWidget(self._key(label), row, 0)
            grid.addWidget(self._value(value), row, 1)
            row += 1

        grid.addWidget(self._separator(), row, 0, 1, 2)
        row += 1

        for label, amount in (
            ("Item", checkout.item_subtotal),
            ("Shipping", checkout.shipping),
            ("Tax", checkout.tax),
            ("Promotion", checkout.promotion),
        ):
            if amount is None:
                # Amazon omits rows that do not apply; showing a fabricated
                # zero would imply the app knew something it did not.
                continue
            grid.addWidget(self._key(label), row, 0)
            grid.addWidget(self._value(amount.format()), row, 1)
            row += 1

        grid.addWidget(self._separator(), row, 0, 1, 2)
        row += 1

        total_key = QLabel("Total")
        total_key.setFont(font_metric())
        total_key.setProperty("role", "metricLabel")
        grid.addWidget(total_key, row, 0)

        total_value = QLabel(
            self._review.order_total.format()
            if self._review.order_total
            else "Not shown"
        )
        total_value.setFont(font_metric())
        total_value.setProperty("role", "metric")
        total_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        total_value.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        grid.addWidget(total_value, row, 1)

        card.add_layout(grid)

        limit = self._review.rules.max_order_total
        if limit is not None:
            note = QLabel(f"Your maximum order total is {limit.format()}.")
            note.setProperty("role", "caption")
            card.add_widget(note)
        return card

    def _build_delivery_card(self) -> Card:
        checkout = self._review.checkout
        card = Card(title="Where it goes and how it is paid")
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)

        grid.addWidget(self._key("Delivering to"), 0, 0)
        address = self._value(checkout.address_label or "Not shown")
        address.setWordWrap(True)
        grid.addWidget(address, 0, 1)

        grid.addWidget(self._key("Paying with"), 1, 0)
        grid.addWidget(self._value(checkout.payment_label or "Not shown"), 1, 1)

        if self._review.snapshot.delivery_estimate:
            grid.addWidget(self._key("Arriving"), 2, 0)
            estimate = self._value(self._review.snapshot.delivery_estimate)
            estimate.setWordWrap(True)
            grid.addWidget(estimate, 2, 1)

        card.add_layout(grid)

        card.add_widget(self._cart_note())
        return card

    def _cart_note(self) -> QLabel:
        """Say what was done about the rest of the user's cart."""
        text = {
            CartStrategy.BUY_NOW: (
                "This is a separate order for this item only. Your Amazon cart "
                "has not been changed."
            ),
            CartStrategy.EMPTY_CART: (
                "Your Amazon cart was empty, so this order contains only this "
                "item."
            ),
            CartStrategy.SET_ASIDE_OTHERS: (
                "Your other cart items were moved to Saved for later and will "
                "be put back after this order."
            ),
            CartStrategy.BLOCKED: "",
        }.get(self._review.strategy, "")
        label = QLabel(text)
        label.setProperty("role", "caption")
        label.setWordWrap(True)
        label.setVisible(bool(text))
        return label

    def _build_guard_card(self) -> Card:
        card = Card(title="Purchase Guard")
        badge = StatusBadge()
        if self._review.report.passed:
            badge.set_status("All checks passed", StatusSeverity.SUCCESS)
        else:
            badge.set_status("Blocked", StatusSeverity.BLOCKED)
        card.set_header_action(badge)

        table = GuardTable()
        table.set_report(self._review.report)
        card.add_widget(table)
        return card

    def _build_footer(self) -> QWidget:
        footer = QWidget()
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(20, 12, 20, 16)
        layout.setSpacing(12)

        cancel = SubtleButton("Cancel")
        cancel.setDefault(True)
        cancel.setAutoDefault(True)
        cancel.clicked.connect(self.reject)

        total = self._review.order_total
        place = PrimaryButton(
            f"Place order - {total.format()}" if total else "Place order"
        )
        place.setAutoDefault(False)
        place.setDefault(False)
        place.clicked.connect(self._on_place_order)

        if not self._review.can_confirm:
            place.setEnabled(False)
            reason = (
                "The safety checks did not pass, so this order cannot be placed."
                if not self._review.report.passed
                else "Amazon's order total could not be read, so this order "
                "cannot be placed."
            )
            place.setToolTip(reason)
            note = QLabel(reason)
            note.setProperty("role", "caption")
            note.setWordWrap(True)
            layout.addWidget(note, 1)
        else:
            layout.addStretch(1)

        layout.addWidget(ButtonRow(cancel, place, align="right"))
        # Escape must never place an order.
        self.setTabOrder(cancel, place)
        return footer

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def _key(text: str) -> QLabel:
        label = QLabel(text)
        label.setProperty("role", "caption")
        return label

    @staticmethod
    def _value(text: str) -> QLabel:
        label = QLabel(text)
        label.setFont(font_body())
        label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    @staticmethod
    def _separator() -> QWidget:
        from app.ui.components import HLine

        return HLine()

    def _on_place_order(self) -> None:
        logger.warning(
            "User confirmed a purchase",
            extra={
                "purchase_job_id": self._review.purchase_job_id,
                "total_cents": (
                    self._review.order_total.cents if self._review.order_total else None
                ),
            },
        )
        self.accept()

    @property
    def review(self) -> PurchaseReview:
        return self._review

    @property
    def approved_total(self) -> Money | None:
        return self._review.order_total
