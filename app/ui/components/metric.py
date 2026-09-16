"""The dashboard metric tile: one number, one caption, optionally clickable.

A tile that can be clicked is a control, not decoration, so it takes keyboard
focus and activates on Enter and Space. Making a whole surface clickable with
only ``mousePressEvent`` is the standard way to build something a keyboard
user cannot reach at all, and there is no visual difference to warn anyone.

The severity is shown as a badge with a word in it rather than by tinting the
number: the stylesheet has no coloured-metric role, and a number that is
simply red says "something" rather than "attention".
"""

from __future__ import annotations

from typing import Final

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QWidget

from app.ui.components.badge import StatusBadge
from app.ui.components.card import Card
from app.ui.components.common import role_label
from app.ui.theme import StatusSeverity

#: Keys that activate a focused tile, matching Windows' own button behaviour.
_ACTIVATION_KEYS: Final[frozenset[int]] = frozenset(
    {
        int(Qt.Key.Key_Return),
        int(Qt.Key.Key_Enter),
        int(Qt.Key.Key_Space),
    }
)


class MetricTile(Card):
    """A big number with a caption under it, e.g. ``7`` / ``Items watched``."""

    #: Emitted on click, Enter or Space -- only when ``clickable`` was set.
    clicked = Signal()

    def __init__(
        self,
        value: str = "-",
        caption: str = "",
        icon: str | None = None,
        clickable: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(icon=icon, parent=parent)
        self._clickable = clickable

        self._value_label = role_label(value, "metric")
        self._caption_label = role_label(caption, "metricLabel")
        self._caption_label.setWordWrap(True)
        self._badge = StatusBadge()
        self._badge.setVisible(False)

        badge_row = QHBoxLayout()
        badge_row.setContentsMargins(0, 0, 0, 0)
        badge_row.addWidget(self._badge)
        badge_row.addStretch(1)

        self.add_widget(self._value_label)
        self.add_widget(self._caption_label)
        self.add_layout(badge_row)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        if clickable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._refresh_accessible_name()

    # -- content ------------------------------------------------------------

    def set_value(self, value: str) -> None:
        """Replace the number. Always a string: ``-`` is a legitimate value."""
        self._value_label.setText(value)
        self._refresh_accessible_name()

    def set_caption(self, caption: str) -> None:
        """Replace the line under the number."""
        self._caption_label.setText(caption)
        self._refresh_accessible_name()

    def set_severity(self, severity: StatusSeverity | None) -> None:
        """Show (or hide) a badge saying how the number should be read."""
        if severity is None:
            self._badge.setVisible(False)
            self._refresh_accessible_name()
            return
        self._badge.set_status(severity.label, severity)
        self._badge.setVisible(True)
        self._refresh_accessible_name()

    def value(self) -> str:
        """The number currently shown."""
        return self._value_label.text()

    def caption(self) -> str:
        """The caption currently shown."""
        return self._caption_label.text()

    # -- activation ---------------------------------------------------------

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        """Activate on a left-button release inside the tile.

        Release rather than press, and only when the cursor is still over the
        tile, so a click that starts here and drags away is cancelled -- the
        same forgiveness a real button gives.
        """
        if (
            self._clickable
            and event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            event.accept()
            self.clicked.emit()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        """Activate on Enter or Space when focused."""
        if self._clickable and int(event.key()) in _ACTIVATION_KEYS:
            event.accept()
            self.clicked.emit()
            return
        super().keyPressEvent(event)

    # -- internals ----------------------------------------------------------

    def _refresh_accessible_name(self) -> None:
        """Announce the tile as one phrase, e.g. "Items watched: 7, Attention".

        A screen reader reading three sibling labels gives "7", "Items
        watched", "Attention" in whatever order it finds them; the tile is one
        piece of information and is named as one.
        """
        # ``isHidden`` rather than ``isVisible``: nothing in a window that has
        # not been shown yet is "visible", and tiles are named while the
        # dashboard is still being assembled.
        parts = [
            part
            for part in (
                self._caption_label.text(),
                self._value_label.text(),
                "" if self._badge.isHidden() else self._badge.text(),
            )
            if part
        ]
        self.setAccessibleName(", ".join(parts))
