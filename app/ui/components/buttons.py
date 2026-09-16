"""The button vocabulary: primary, subtle, danger, icon.

Three visual weights exist so that a screen can only ever have one obvious
action. The classes are deliberately thin -- an ``objectName``, a minimum
height, a cursor and an accessible name -- because the drawing belongs to the
stylesheet and the behaviour belongs to the page.

The accessible name is derived from the text and re-derived whenever the text
changes. Left to Qt, a button whose label is set after construction reports the
name it was born with, which is how a screen reader ends up announcing "Pause"
for a button that now says "Resume".
"""

from __future__ import annotations

from typing import Final

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy, QWidget

from app.ui.components.common import icon_set

#: Minimum button height. Windows 11's own buttons are 32px tall, and the
#: stylesheet's padding alone does not reach it for a one-word label.
MIN_HEIGHT: Final[int] = 32

#: Icon-only buttons are square at the same height, with a 16px glyph.
ICON_BUTTON_SIZE: Final[int] = 32
ICON_GLYPH_SIZE: Final[int] = 16


class _NamedButton(QPushButton):
    """Shared plumbing: a stylesheet identity and a name that tracks the text."""

    #: Set by each subclass; the stylesheet selects on it.
    OBJECT_NAME: str = ""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName(self.OBJECT_NAME)
        self.setMinimumHeight(MIN_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        """Change the label, and the accessible name with it."""
        super().setText(text)
        # An icon-only button sets its own name from the tooltip; do not let an
        # empty label wipe it.
        if text:
            self.setAccessibleName(text)


class PrimaryButton(_NamedButton):
    """The one action a screen wants the user to take."""

    OBJECT_NAME = "PrimaryButton"


class SubtleButton(_NamedButton):
    """A tertiary action: "Open on Amazon", "Clear", "Show details"."""

    OBJECT_NAME = "SubtleButton"


class DangerButton(_NamedButton):
    """An irreversible action: delete a watch, sign out, clear history."""

    OBJECT_NAME = "DangerButton"


class IconButton(SubtleButton):
    """A square, icon-only button for row and toolbar actions.

    An icon on its own is not a label, so the tooltip is mandatory and is used
    as the accessible name too. Without it the control is announced as
    "button" and is unusable with a screen reader.
    """

    def __init__(
        self,
        icon_name: str,
        tooltip: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("", parent)
        self._icon_name = icon_name
        self.setIcon(icon_set().icon(icon_name))
        self.setIconSize(QSize(ICON_GLYPH_SIZE, ICON_GLYPH_SIZE))
        self.setFixedSize(ICON_BUTTON_SIZE, ICON_BUTTON_SIZE)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setAccessibleDescription(tooltip)

    def refresh_theme(self) -> None:
        """Re-rasterise the glyph in the new theme's ink."""
        self.setIcon(icon_set().icon(self._icon_name))


class ButtonRow(QWidget):
    """A horizontal strip of buttons, aligned as a group.

    ``align`` is ``"right"`` (the Windows convention for a dialog's actions),
    ``"left"`` or ``"center"``. An unknown value is treated as ``"right"``
    rather than raising: a mistyped alignment should not stop a page opening.
    """

    def __init__(
        self,
        *buttons: QWidget,
        align: str = "right",
        spacing: int = 8,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(spacing)
        if align in {"right", "center"}:
            row.addStretch(1)
        for button in buttons:
            row.addWidget(button)
        if align in {"left", "center"}:
            row.addStretch(1)
        self._buttons = tuple(buttons)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    @property
    def buttons(self) -> tuple[QWidget, ...]:
        """The buttons in the row, in order."""
        return self._buttons
