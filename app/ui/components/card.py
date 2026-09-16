"""The card: the only container in this application that draws a surface.

Every screen is a column of cards, so the margins, the spacing and the header
hairline are defined once here instead of being re-typed per page, where they
would drift by two pixels at a time.

The header is built lazily. ``#CardHeader`` carries a bottom hairline, and a
card with a header but no title would show that rule floating above its
content, which reads as a rendering fault rather than as a design.
"""

from __future__ import annotations

from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.ui.components.common import enable_styled_background, icon_set, role_label

#: Card padding. 16px matches the stylesheet's ``spaceLg`` and Windows 11's own
#: card inset; the body gap is one step smaller so a card reads as one block.
_MARGIN: Final[int] = 16
_BODY_SPACING: Final[int] = 8
_HEADER_GAP: Final[int] = 12

#: Header icon size. 20px is the smallest size the glyphs were drawn for that
#: still has the same optical weight as 13.5pt title text next to it.
_ICON_SIZE: Final[int] = 20


class Card(QFrame):
    """A titled surface that callers fill with their own widgets.

    Content goes into :attr:`body_layout` (or through :meth:`add_widget` and
    :meth:`add_layout`). The header, when there is one, holds an optional icon,
    the title, the subtitle and a right-aligned action slot.
    """

    def __init__(
        self,
        title: str | None = None,
        subtitle: str | None = None,
        icon: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        # NoFrame: the border comes from the stylesheet, and QFrame's own
        # shape would draw a second, native one inside it.
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(_MARGIN, _MARGIN, _MARGIN, _MARGIN)
        self._outer.setSpacing(_HEADER_GAP)

        self._body = QVBoxLayout()
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(_BODY_SPACING)
        self._outer.addLayout(self._body)

        self._header: QFrame | None = None
        self._header_row: QHBoxLayout | None = None
        self._header_column: QVBoxLayout | None = None
        self._icon_label: QLabel | None = None
        self._icon_name: str | None = None
        self._title_label: QLabel | None = None
        self._subtitle_label: QLabel | None = None
        self._action: QWidget | None = None

        if icon is not None:
            self.set_icon(icon)
        if title is not None:
            self.set_title(title)
        if subtitle is not None:
            self.set_subtitle(subtitle)

    # -- content ------------------------------------------------------------

    @property
    def body_layout(self) -> QVBoxLayout:
        """The layout page code adds its content to."""
        return self._body

    def add_widget(self, widget: QWidget) -> None:
        """Append ``widget`` to the card body."""
        self._body.addWidget(widget)

    def add_layout(self, layout: QLayout) -> None:
        """Append a nested ``layout`` to the card body."""
        self._body.addLayout(layout)

    # -- header -------------------------------------------------------------

    def set_title(self, title: str | None) -> None:
        """Set or clear the card's title."""
        if title is None:
            if self._title_label is not None:
                self._title_label.setVisible(False)
            return
        label = self._title_label
        if label is None:
            label = role_label(title, "title")
            self._title_label = label
            self._text_column().addWidget(label)
        label.setText(title)
        label.setVisible(True)
        self.setAccessibleName(title)

    def set_subtitle(self, subtitle: str | None) -> None:
        """Set or clear the explanatory line under the title."""
        if subtitle is None:
            if self._subtitle_label is not None:
                self._subtitle_label.setVisible(False)
            return
        label = self._subtitle_label
        if label is None:
            label = role_label(subtitle, "subtitle")
            label.setWordWrap(True)
            self._subtitle_label = label
            self._text_column().addWidget(label)
        label.setText(subtitle)
        label.setVisible(True)

    def set_icon(self, name: str | None) -> None:
        """Show the named theme icon before the title, or remove it."""
        self._icon_name = name
        if name is None:
            if self._icon_label is not None:
                self._icon_label.setVisible(False)
            return
        label = self._icon_label
        if label is None:
            label = QLabel()
            label.setFixedSize(_ICON_SIZE, _ICON_SIZE)
            self._icon_label = label
            self._ensure_header().insertWidget(0, label, 0, Qt.AlignmentFlag.AlignTop)
        label.setPixmap(icon_set().pixmap(name, _ICON_SIZE))
        label.setVisible(True)

    def refresh_theme(self) -> None:
        """Re-rasterise the header icon in the current text colour.

        The glyph was drawn in the old theme's ink and would otherwise stay
        dark on a dark card. Called by
        :func:`app.ui.components.common.apply_theme`.
        """
        if self._icon_name is not None:
            self.set_icon(self._icon_name)

    def set_header_action(self, button: QWidget) -> None:
        """Put ``button`` at the right-hand end of the header row.

        Replaces any previous action, so a page can swap "Pause" for "Resume"
        without leaving both behind.
        """
        row = self._ensure_header()
        if self._action is not None:
            row.removeWidget(self._action)
            self._action.setParent(None)
        self._action = button
        row.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)

    # -- internals ----------------------------------------------------------

    def _ensure_header(self) -> QHBoxLayout:
        """The header row, created on first use and inserted above the body."""
        if self._header_row is not None:
            return self._header_row
        header = QFrame(self)
        header.setObjectName("CardHeader")
        header.setFrameShape(QFrame.Shape.NoFrame)
        enable_styled_background(header)
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(_BODY_SPACING)
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        row.addLayout(column, 1)
        self._header = header
        self._header_row = row
        self._header_column = column
        self._outer.insertWidget(0, header)
        return row

    def _text_column(self) -> QVBoxLayout:
        """The title/subtitle column inside the header."""
        self._ensure_header()
        column = self._header_column
        assert column is not None  # _ensure_header always sets it
        return column
