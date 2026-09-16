"""Resolving an order whose outcome could not be read.

This is the dialog for the worst case the application can reach: the order
button was clicked, but Amazon's confirmation could not be identified. An
order may exist and it may not, and the program deliberately does not guess
or retry.

The design follows from that:

* It says plainly that the app will not try again.
* It offers to look the order up, which only reads the order history.
* It asks the user to choose one of two specific answers. There is no
  "dismiss" that leaves the question open, because an unresolved purchase
  blocks any further purchase of that product -- which is the safe state, but
  the user needs to know why.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.branding import BRAND
from app.core.money import Money
from app.ui.components import ButtonRow, Card, DangerButton, PrimaryButton, SubtleButton
from app.ui.theme import StatusSeverity, font_body, font_title
from app.ui.components import StatusBadge

logger = logging.getLogger("app.ui.dialogs.uncertain")


class UncertainOrderDialog(QDialog):
    """Asks the user what happened to a possible order."""

    #: Emitted when the user asks the app to look the order up for them.
    check_orders_requested = Signal()

    def __init__(
        self,
        *,
        product_title: str,
        expected_total: Money | None,
        detail: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._answer: bool | None = None
        self._note = ""

        self.setWindowTitle(f"Did this order go through? - {BRAND.display_name}")
        self.setModal(True)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        heading = QLabel("The result of this order is not certain")
        heading.setFont(font_title())
        heading.setProperty("role", "title")
        heading.setWordWrap(True)
        layout.addWidget(heading)

        badge = StatusBadge()
        badge.set_status("The app will not try again", StatusSeverity.WARNING)
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignLeft)

        explanation = QLabel(
            f"The order for {product_title} was submitted to Amazon, but "
            "Amazon's confirmation could not be read. It may have gone "
            "through, and it may not have. To make sure you are never charged "
            "twice, the app will not submit it again."
        )
        explanation.setFont(font_body())
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        if detail:
            note = QLabel(detail)
            note.setProperty("role", "caption")
            note.setWordWrap(True)
            layout.addWidget(note)

        instruction = Card(title="What to do")
        step = QLabel(
            "Open your Amazon orders and look for this item"
            + (
                f", with a total of {expected_total.format()}."
                if expected_total
                else "."
            )
            + " Then tell the app what you found."
        )
        step.setFont(font_body())
        step.setWordWrap(True)
        instruction.add_widget(step)

        look_up = SubtleButton("Check my Amazon orders")
        look_up.setAccessibleName("Check my Amazon orders")
        look_up.clicked.connect(self.check_orders_requested)
        instruction.add_widget(look_up)
        layout.addWidget(instruction)

        notes_label = QLabel("Anything you want to note (optional)")
        notes_label.setProperty("role", "caption")
        layout.addWidget(notes_label)
        self._notes = QPlainTextEdit()
        self._notes.setPlaceholderText(
            "For example: found order 112-1234567-7654321"
        )
        self._notes.setFixedHeight(64)
        self._notes.setAccessibleName("Notes about this order")
        layout.addWidget(self._notes)

        layout.addStretch(1)

        not_placed = SubtleButton("No order was placed")
        not_placed.setAccessibleName("No order was placed")
        not_placed.clicked.connect(lambda: self._answer_with(False))

        was_placed = DangerButton("Yes, the order was placed")
        was_placed.setAccessibleName("Yes, the order was placed")
        was_placed.setAutoDefault(False)
        was_placed.clicked.connect(lambda: self._answer_with(True))

        later = PrimaryButton("Decide later")
        later.setDefault(True)
        later.setToolTip(
            "This item cannot be purchased again until you answer, which keeps "
            "you from being charged twice."
        )
        later.clicked.connect(self.reject)

        layout.addWidget(ButtonRow(not_placed, was_placed, later, align="right"))

        logger.warning(
            "Asking the user to resolve an uncertain order",
            extra={"product": product_title},
        )

    def _answer_with(self, placed: bool) -> None:
        self._answer = placed
        self._note = self._notes.toPlainText().strip()
        logger.warning(
            "User resolved an uncertain order",
            extra={"order_was_placed": placed},
        )
        self.accept()

    @property
    def order_was_placed(self) -> bool | None:
        """``True``/``False`` once answered, ``None`` if deferred."""
        return self._answer

    @property
    def note(self) -> str:
        return self._note or (
            "You confirmed the order was placed."
            if self._answer
            else "You confirmed no order was placed."
        )
