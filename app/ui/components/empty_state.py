"""The empty state: what a screen shows before it has anything to show.

An empty list that is simply blank looks broken, and the user's next move is
to restart the application. So every list in this application has one of these
instead: an icon, a heading that says what is missing, one sentence that says
how to fix it, and -- where there is an obvious next step -- the button that
takes it.

The dashed outline is the stylesheet's, and it is load-bearing: a dashed
border reads as a placeholder, while the solid border of a card would read as
a card that failed to load.
"""

from __future__ import annotations

from typing import Final

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from app.ui.components.buttons import PrimaryButton
from app.ui.components.common import enable_styled_background, icon_set, role_label

#: Large enough to read as an illustration rather than as a button's icon.
_ICON_SIZE: Final[int] = 32


class EmptyState(QWidget):
    """A centred icon, heading, sentence and optional action."""

    #: Emitted when the action button is pressed. Never emitted when the
    #: state was built without ``action_text``.
    action_clicked = Signal()

    def __init__(
        self,
        icon: str | None = None,
        heading: str = "",
        body: str = "",
        action_text: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("EmptyState")
        # A bare QWidget paints no background, so the stylesheet's dashed
        # outline would not appear without this.
        enable_styled_background(self)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        column.addStretch(1)

        self._icon_name: str | None = None
        self._icon_label = QLabel(self)
        self._icon_label.setFixedSize(_ICON_SIZE, _ICON_SIZE)
        self._icon_label.setVisible(False)
        column.addWidget(self._icon_label, 0, Qt.AlignmentFlag.AlignHCenter)

        self._heading = role_label(heading, "title", self)
        self._heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._heading.setWordWrap(True)
        column.addWidget(self._heading)

        self._body = role_label(body, "caption", self)
        self._body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._body.setWordWrap(True)
        column.addWidget(self._body)

        self._button: PrimaryButton | None = None
        if action_text:
            self._button = PrimaryButton(action_text, self)
            self._button.clicked.connect(self.action_clicked.emit)
            column.addWidget(self._button, 0, Qt.AlignmentFlag.AlignHCenter)

        column.addStretch(1)

        if icon is not None:
            self.set_icon(icon)
        self._refresh_accessible_name()

    # -- content ------------------------------------------------------------

    def set_icon(self, name: str | None) -> None:
        """Show the named theme icon, or hide the illustration entirely."""
        self._icon_name = name
        if name is None:
            self._icon_label.setVisible(False)
            return
        # Muted, not full-strength text: the icon is context, and the heading
        # is the thing to read first.
        self._icon_label.setPixmap(icon_set().pixmap(name, _ICON_SIZE, "textMuted"))
        self._icon_label.setVisible(True)

    def set_text(self, heading: str, body: str = "") -> None:
        """Replace the heading and the explanatory sentence."""
        self._heading.setText(heading)
        self._body.setText(body)
        self._body.setVisible(bool(body))
        self._refresh_accessible_name()

    @property
    def heading(self) -> str:
        """The heading currently shown."""
        return self._heading.text()

    @property
    def body(self) -> str:
        """The explanatory sentence currently shown."""
        return self._body.text()

    @property
    def action_button(self) -> PrimaryButton | None:
        """The action button, when this state was given one."""
        return self._button

    def refresh_theme(self) -> None:
        """Re-rasterise the illustration in the new theme's muted ink."""
        if self._icon_name is not None:
            self.set_icon(self._icon_name)

    # -- internals ----------------------------------------------------------

    def _refresh_accessible_name(self) -> None:
        parts = [part for part in (self._heading.text(), self._body.text()) if part]
        self.setAccessibleName(". ".join(parts))
