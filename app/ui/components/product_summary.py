"""The product card: what the program believes it is about to buy.

This card is the user's chance to catch a mistake before money moves, so its
governing rule is that **it never guesses**. A :class:`ProductSnapshot` carries
``None`` or ``UNKNOWN`` for anything the page did not state clearly, and those
arrive here as a dash or as "Not shown" -- never as a plausible-looking
default, and never as an empty gap, because a blank row is indistinguishable
from a row the user simply failed to read.

Every value is labelled. "Amazon.com" on its own tells you nothing about
whether it is the seller or the shipper, and those are different rules.
"""

from __future__ import annotations

import logging
from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.purchasing.models import ProductSnapshot
from app.ui.components.card import Card
from app.ui.components.common import ElidingLabel, icon_set, role_label

_LOG: Final = logging.getLogger("app.ui.components.product_summary")

#: The thumbnail box. Fixed, and the image is letterboxed inside it, so a row
#: of cards does not jog left and right as portrait and landscape images load.
THUMBNAIL_SIZE: Final[int] = 96

#: Shown where a value is missing. A dash is unmistakably "nothing here",
#: which an empty cell is not.
DASH: Final[str] = "-"

#: Shown for a missing price specifically. "Not shown" says the page did not
#: state one; a dash next to a currency field reads like a zero.
NO_PRICE: Final[str] = "Not shown"

#: Title lines before eliding. Two lines is enough to tell two variants of the
#: same product apart, which one line often is not.
TITLE_LINES: Final[int] = 2


class ProductSummaryCard(Card):
    """An image, a title and a labelled grid of everything that was read.

    ``compact`` drops the grid to price and availability only, for list rows
    where the full detail would bury the list.
    """

    def __init__(self, compact: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self._compact = compact
        self._snapshot: ProductSnapshot | None = None

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        self._image = QLabel(self)
        self._image.setFixedSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE)
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image.setAccessibleName("Product image")
        row.addWidget(self._image, 0, Qt.AlignmentFlag.AlignTop)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        self._title = ElidingLabel("", "title", TITLE_LINES, self)
        column.addWidget(self._title)

        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(12)
        self._grid.setVerticalSpacing(2)
        self._grid.setColumnStretch(1, 1)
        column.addLayout(self._grid)
        column.addStretch(1)
        row.addLayout(column, 1)

        self.add_layout(row)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        self._has_image = False
        self._rows: dict[str, tuple[QLabel, ElidingLabel]] = {}
        for key in self._field_keys():
            self._add_field(key)
        self._show_placeholder()

    # -- public API ---------------------------------------------------------

    def set_product(
        self, snapshot: ProductSnapshot, image: QPixmap | None = None
    ) -> None:
        """Render ``snapshot``, with ``image`` as the thumbnail when supplied."""
        self._snapshot = snapshot
        self._title.setText(snapshot.display_title)
        self.setAccessibleName(snapshot.display_title)

        values = {
            "Item code": snapshot.asin or DASH,
            "Version": snapshot.variation.describe_full(),
            "Price": snapshot.price.format() if snapshot.price else NO_PRICE,
            "Availability": snapshot.availability.label,
            "Sold by": snapshot.seller or DASH,
            "Ships from": snapshot.ships_from or DASH,
            "Condition": snapshot.condition.label,
            "Delivery": snapshot.delivery_estimate or DASH,
            # Only stated when Amazon positively said so. ``None`` means the
            # page did not say, which is not the same as "not eligible".
            "Prime": "Eligible" if snapshot.prime_eligible is True else "",
        }
        for key, (caption, value_label) in self._rows.items():
            text = values.get(key, DASH)
            visible = bool(text)
            caption.setVisible(visible)
            value_label.setVisible(visible)
            if visible:
                value_label.setText(text)

        self.set_image(image)

    def set_image(self, image: QPixmap | None) -> None:
        """Show ``image`` letterboxed in the thumbnail box, or the placeholder."""
        if image is None or image.isNull():
            self._show_placeholder()
            return
        self._has_image = True
        self._image.setPixmap(
            image.scaled(
                THUMBNAIL_SIZE,
                THUMBNAIL_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def set_image_from_bytes(self, data: bytes) -> bool:
        """Decode ``data`` as the thumbnail. Returns whether it worked.

        Product images come off the network, so "not an image" is a normal
        outcome, not an exception: a truncated download or an error page in
        place of a JPEG must leave the card readable rather than raise inside
        a paint or a network callback.
        """
        pixmap = QPixmap()
        if not data or not pixmap.loadFromData(data):
            _LOG.debug("Product image could not be decoded (%d bytes)", len(data or b""))
            self._show_placeholder()
            return False
        self.set_image(pixmap)
        return True

    def field_text(self, key: str) -> str | None:
        """The value shown for a labelled field, or ``None`` when hidden.

        Returns the value in full even when the label is eliding it on screen.
        ``isHidden`` rather than ``isVisible``: a widget in a window that has
        not been shown yet is not visible but is certainly not hidden, and the
        purchase dialog asks this question while it is still being built.
        """
        pair = self._rows.get(key)
        if pair is None or pair[1].isHidden():
            return None
        return pair[1].full_text

    @property
    def snapshot(self) -> ProductSnapshot | None:
        """The snapshot currently displayed."""
        return self._snapshot

    @property
    def title_text(self) -> str:
        """The full, un-elided product title."""
        return self._title.full_text

    # -- internals ----------------------------------------------------------

    def _field_keys(self) -> tuple[str, ...]:
        """The rows this card shows, in reading order."""
        if self._compact:
            return ("Price", "Availability")
        return (
            "Item code",
            "Version",
            "Price",
            "Availability",
            "Sold by",
            "Ships from",
            "Condition",
            "Delivery",
            "Prime",
        )

    def _add_field(self, key: str) -> None:
        index = self._grid.rowCount()
        caption = role_label(key, "caption", self)
        caption.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
        )
        # Seller names and delivery sentences are long and arbitrary, so the
        # value elides rather than stretching the card off the screen.
        value = ElidingLabel(DASH, None, 1, self)
        value.setAccessibleName(key)
        self._grid.addWidget(caption, index, 0)
        self._grid.addWidget(value, index, 1)
        self._rows[key] = (caption, value)

    def refresh_theme(self) -> None:
        """Re-draw the placeholder glyph in the new theme's ink."""
        if not self._has_image:
            self._show_placeholder()

    def _show_placeholder(self) -> None:
        """A muted glyph where the image would be.

        Chosen over an empty box because an empty box looks like an image that
        is still loading, and the user waits for it.
        """
        self._has_image = False
        self._image.setPixmap(
            icon_set().pixmap("cart", THUMBNAIL_SIZE // 2, "textDisabled")
        )
