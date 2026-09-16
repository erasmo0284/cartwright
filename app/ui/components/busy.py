"""Progress feedback for work that happens in another thread.

Both widgets here are *indeterminate* on purpose. The work they report on --
opening a browser, loading a product page, reading a price -- has no
measurable percentage, and a progress bar that invents one is a lie the user
learns to distrust. The message carries the information ("Reading price..."),
the animation only says "still going".

Neither widget spins the event loop, sleeps, or calls ``processEvents``: the
automation runs in a worker and these react to its signals. A "busy"
indicator that blocks the interface it is drawn on is the defect it exists to
prevent.
"""

from __future__ import annotations

from typing import Final

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.ui.components.buttons import SubtleButton
from app.ui.components.common import role_label

#: Bar thickness. Thin enough to read as a status strip rather than as a
#: dialog's progress bar, which is what it is: a strip along one card.
BAR_HEIGHT: Final[int] = 4

#: Overlay tint opacity, out of 255. Enough to push the page behind it clearly
#: back, not so much that the user loses the context they were working in.
OVERLAY_ALPHA: Final[int] = 210


class ProgressStrip(QWidget):
    """A thin indeterminate bar, a step message and an optional Cancel.

    Hidden whenever there is nothing happening: a permanently visible strip
    showing "Idle" is noise, and the user stops seeing it before the one time
    it matters.
    """

    #: Emitted when the user presses Cancel. The strip does not stop itself --
    #: the worker decides when it has actually stopped, and calls :meth:`stop`.
    cancel_requested = Signal()

    def __init__(
        self, cancellable: bool = True, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self._message = role_label("", "caption", self)
        self._message.setWordWrap(True)
        row.addWidget(self._message, 1)

        self._cancel: SubtleButton | None = None
        if cancellable:
            self._cancel = SubtleButton("Cancel", self)
            self._cancel.setAccessibleName("Cancel the current step")
            self._cancel.clicked.connect(self.cancel_requested.emit)
            row.addWidget(self._cancel, 0)
        column.addLayout(row)

        self._bar = QProgressBar(self)
        # A zero-width range is Qt's own way of saying "indeterminate"; the
        # stylesheet leaves QProgressBar alone, so this is the native busy
        # animation rather than a hand-drawn one.
        self._bar.setRange(0, 0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(BAR_HEIGHT)
        self._bar.setAccessibleName("Working")
        column.addWidget(self._bar)

        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setVisible(False)

    # -- public API ---------------------------------------------------------

    def start(self, message: str) -> None:
        """Show the strip with ``message`` as the current step."""
        self.set_message(message)
        self.setVisible(True)

    def set_message(self, message: str) -> None:
        """Replace the step message without changing visibility."""
        self._message.setText(message)
        self.setAccessibleName(message or "Working")
        self.setAccessibleDescription(message)

    def stop(self) -> None:
        """Hide the strip and forget the message."""
        self.setVisible(False)
        self.set_message("")

    def is_running(self) -> bool:
        """Whether the strip is currently showing.

        ``isHidden`` rather than ``isVisible``: the strip may legitimately be
        started while its own page is not on screen yet.
        """
        return not self.isHidden()

    @property
    def message(self) -> str:
        """The step message currently shown."""
        return self._message.text()

    @property
    def cancel_button(self) -> SubtleButton | None:
        """The Cancel button, when this strip was built with one."""
        return self._cancel


class BusyOverlay(QWidget):
    """A translucent sheet over a page, for a wait that blocks the whole view.

    Used where nothing on the page can be trusted while the work runs -- a
    checkout being re-read, for instance. Because it covers its host, the host
    keeps its own layout untouched and gets it back intact.

    The tint is painted rather than styled: the application stylesheet has no
    translucency, and giving one widget its own would break the rule that all
    colour comes from the theme. The fill is the palette's own window colour
    with an alpha, so it follows light and dark for free.
    """

    #: Emitted when the user presses Cancel, when there is one.
    cancel_requested = Signal()

    def __init__(
        self,
        message: str = "Working...",
        cancellable: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._host: QWidget | None = None

        column = QVBoxLayout(self)
        column.setContentsMargins(24, 24, 24, 24)
        column.setSpacing(12)
        column.addStretch(1)

        self._message = role_label(message, "sectionHeading", self)
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)
        column.addWidget(self._message)

        self._bar = QProgressBar(self)
        self._bar.setRange(0, 0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(BAR_HEIGHT)
        self._bar.setMaximumWidth(240)
        self._bar.setAccessibleName("Working")
        column.addWidget(self._bar, 0, Qt.AlignmentFlag.AlignHCenter)

        self._cancel: SubtleButton | None = None
        if cancellable:
            self._cancel = SubtleButton("Cancel", self)
            self._cancel.clicked.connect(self.cancel_requested.emit)
            column.addWidget(self._cancel, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addStretch(1)

        self.setAccessibleName(message)
        self.setVisible(False)

    # -- public API ---------------------------------------------------------

    def set_message(self, message: str) -> None:
        """Replace the message shown in the middle of the sheet."""
        self._message.setText(message)
        self.setAccessibleName(message)

    def show_over(self, parent_widget: QWidget) -> None:
        """Cover ``parent_widget`` completely and show the sheet.

        An event filter on the host keeps the geometry correct: a window
        resize would otherwise leave the sheet at its old size, with part of
        the page it is meant to be blocking usable again.
        """
        if self._host is not None and self._host is not parent_widget:
            self._host.removeEventFilter(self)
        self._host = parent_widget
        self.setParent(parent_widget)
        parent_widget.installEventFilter(self)
        self.setGeometry(parent_widget.rect())
        self.raise_()
        self.setVisible(True)

    def hide_overlay(self) -> None:
        """Hide the sheet and stop following its host."""
        if self._host is not None:
            self._host.removeEventFilter(self)
            self._host = None
        self.setVisible(False)

    def is_showing(self) -> bool:
        """Whether the sheet is currently covering a host."""
        return not self.isHidden()

    @property
    def message(self) -> str:
        """The message currently shown."""
        return self._message.text()

    # -- Qt overrides -------------------------------------------------------

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        """Track the host's size so the sheet always covers all of it."""
        if (
            watched is self._host
            and event.type() == QEvent.Type.Resize
            and self._host is not None
        ):
            self.setGeometry(self._host.rect())
        return super().eventFilter(watched, event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt naming
        """Fill the sheet with a translucent wash of the page colour."""
        painter = QPainter(self)
        tint = QColor(self.palette().color(self.backgroundRole()))
        tint.setAlpha(OVERLAY_ALPHA)
        painter.fillRect(self.rect(), tint)
        painter.end()
        super().paintEvent(event)
