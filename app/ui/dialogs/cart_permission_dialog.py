"""Asking permission to move the user's other cart items out of the way.

Amazon's "Proceed to checkout" takes the entire active cart, so buying one
item out of a full cart requires temporarily setting the others aside. That
touches the user's own data, so it is never done silently: this dialog lists
exactly what would be moved, says it will be put back, and defaults to
cancelling.

Buy Now avoids the problem entirely, so this dialog only appears when Amazon
did not offer Buy Now for the item.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QListWidget, QVBoxLayout, QWidget

from app.branding import BRAND
from app.purchasing.models import CartLine
from app.ui.components import ButtonRow, PrimaryButton, SubtleButton
from app.ui.theme import font_body, font_title

logger = logging.getLogger("app.ui.dialogs.cart_permission")

#: Above this many items, setting the cart aside is more disruptive than
#: helpful and the user is steered towards doing it themselves.
MANY_ITEMS = 8


class CartPermissionDialog(QDialog):
    """Asks whether the other cart items may be set aside for this order."""

    def __init__(
        self,
        *,
        product_title: str,
        foreign_lines: tuple[CartLine, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Your cart has other items - {BRAND.display_name}")
        self.setModal(True)
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        heading = QLabel("Your Amazon cart has other items in it")
        heading.setFont(font_title())
        heading.setProperty("role", "title")
        heading.setWordWrap(True)
        layout.addWidget(heading)

        count = len(foreign_lines)
        explanation = QLabel(
            f"Amazon did not offer Buy Now for {product_title}, so the only way "
            f"to order it on its own is to move your other "
            f"{'item' if count == 1 else f'{count} items'} to Saved for later "
            "first. They will be put back as soon as the order finishes."
        )
        explanation.setFont(font_body())
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        items = QListWidget()
        items.setAccessibleName("Items that would be set aside")
        items.setMaximumHeight(160)
        for line in foreign_lines:
            quantity = f" (x{line.quantity})" if line.quantity > 1 else ""
            items.addItem(f"{line.display_title}{quantity}")
        layout.addWidget(items)

        if count >= MANY_ITEMS:
            warning = QLabel(
                "That is a lot of items to move. You may prefer to check out "
                "your cart yourself first, then come back."
            )
            warning.setProperty("role", "caption")
            warning.setWordWrap(True)
            layout.addWidget(warning)

        reassurance = QLabel(
            "Nothing in your cart will be ordered. Only the item you chose is "
            "bought."
        )
        reassurance.setProperty("role", "caption")
        reassurance.setWordWrap(True)
        layout.addWidget(reassurance)

        layout.addStretch(1)

        cancel = PrimaryButton("Cancel this purchase")
        cancel.setDefault(True)
        cancel.clicked.connect(self.reject)

        proceed = SubtleButton("Set them aside and continue")
        proceed.setAutoDefault(False)
        proceed.clicked.connect(self.accept)

        layout.addWidget(ButtonRow(proceed, cancel, align="right"))

        logger.info(
            "Asking permission to set cart items aside",
            extra={"foreign_items": count},
        )

    @property
    def permission_granted(self) -> bool:
        return self.result() == QDialog.DialogCode.Accepted
