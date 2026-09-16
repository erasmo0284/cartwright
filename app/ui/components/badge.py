"""Status indicators, and the one place that decides what a status *means*.

The rule these widgets exist to enforce: colour is never the only signal. A
badge always carries words, and the dot -- which is nothing but colour -- is
only ever offered through :class:`StatusLabel`, which pairs it with a label.
Roughly one man in twelve cannot separate the success green from the danger
red, and this application refuses purchases.

:func:`severity_for_watch_status` and :func:`severity_for_activity` live here
so that the mapping from a domain state to a colour exists once. When each
screen keeps its own copy, "needs sign-in" ends up amber on the dashboard and
red in the watch list, and the user reasonably concludes they are different
problems.
"""

from __future__ import annotations

import logging
from typing import Final, Mapping

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

from app.database.records import ActivitySeverity, WatchStatus
from app.ui.components.common import enable_styled_background, set_property
from app.ui.theme import StatusSeverity, font_caption

_LOG: Final = logging.getLogger("app.ui.components.badge")

#: Dot diameter. The stylesheet pins ``#StatusDot`` to 8px, which is the size
#: its corner radius was chosen for; this is the size the dot takes when it is
#: drawn without the application stylesheet in force.
DOT_SIZE: Final[int] = 10

#: Watch lifecycle -> how it should read.
#:
#: ``TARGET_REACHED`` and ``PURCHASE_COMPLETED`` are the two states that mean
#: the job did its job, so both are success. ``WAITING_FOR_PRICE`` and
#: ``OUT_OF_STOCK`` are info, not warnings: nothing is wrong, the conditions
#: simply are not met yet, and an amber row per out-of-stock item would train
#: the user to ignore amber. ``PAUSED`` and ``EXPIRED`` are neutral because
#: they are the user's own decision or a scheduled end, not an incident.
_WATCH_SEVERITY: Final[Mapping[WatchStatus, StatusSeverity]] = {
    WatchStatus.WATCHING: StatusSeverity.SUCCESS,
    WatchStatus.WAITING_FOR_PRICE: StatusSeverity.INFO,
    WatchStatus.TARGET_REACHED: StatusSeverity.SUCCESS,
    WatchStatus.OUT_OF_STOCK: StatusSeverity.INFO,
    WatchStatus.PAUSED: StatusSeverity.NEUTRAL,
    WatchStatus.NEEDS_LOGIN: StatusSeverity.WARNING,
    WatchStatus.NEEDS_VERIFICATION: StatusSeverity.WARNING,
    WatchStatus.PURCHASE_COMPLETED: StatusSeverity.SUCCESS,
    WatchStatus.EXPIRED: StatusSeverity.NEUTRAL,
    WatchStatus.ERROR: StatusSeverity.ERROR,
}

#: Activity feed severity -> badge severity. The two enumerations were defined
#: independently (one is persisted, one is visual) and this is the seam.
_ACTIVITY_SEVERITY: Final[Mapping[ActivitySeverity, StatusSeverity]] = {
    ActivitySeverity.INFO: StatusSeverity.INFO,
    ActivitySeverity.SUCCESS: StatusSeverity.SUCCESS,
    ActivitySeverity.WARNING: StatusSeverity.WARNING,
    ActivitySeverity.BLOCKED: StatusSeverity.BLOCKED,
    ActivitySeverity.ERROR: StatusSeverity.ERROR,
}


def severity_for_watch_status(status: WatchStatus) -> StatusSeverity:
    """How a watch job's state should be coloured.

    An unrecognised state falls back to ``NEUTRAL`` and is logged: a new
    member of :class:`WatchStatus` should not be able to stop a list from
    drawing, and the test suite iterates the enumeration to catch the gap.
    """
    severity = _WATCH_SEVERITY.get(status)
    if severity is None:
        _LOG.warning("No severity mapped for watch status %r", status)
        return StatusSeverity.NEUTRAL
    return severity


def severity_for_activity(severity: ActivitySeverity) -> StatusSeverity:
    """How an activity-feed entry should be coloured."""
    mapped = _ACTIVITY_SEVERITY.get(severity)
    if mapped is None:
        _LOG.warning("No severity mapped for activity severity %r", severity)
        return StatusSeverity.NEUTRAL
    return mapped


class StatusBadge(QLabel):
    """A pill with a word in it, coloured by severity.

    The text is the signal and the colour is the reinforcement, never the
    other way round: :meth:`set_status` requires text, and passing an empty
    string falls back to the severity's own plain-English label.
    """

    def __init__(
        self,
        text: str = "",
        severity: StatusSeverity = StatusSeverity.NEUTRAL,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Badge")
        # Hug the text: in a vertical layout a QLabel would otherwise stretch
        # to the full width and the pill would read as a coloured band.
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setFont(font_caption())
        self.set_status(text or severity.label, severity)

    def set_status(self, text: str, severity: StatusSeverity) -> None:
        """Show ``text`` in ``severity``'s colours."""
        shown = text or severity.label
        self.setText(shown)
        self.setAccessibleName(shown)
        set_property(self, "severity", severity.value)

    @property
    def severity(self) -> StatusSeverity:
        """The severity currently in force."""
        return StatusSeverity(str(self.property("severity")))


class StatusDot(QWidget):
    """A small filled circle, for list rows where a badge is too heavy.

    Never use one on its own -- it carries no text, so on its own it is
    colour-only. :class:`StatusLabel` is the supported way to show it.
    """

    def __init__(
        self,
        severity: StatusSeverity = StatusSeverity.NEUTRAL,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("StatusDot")
        # A bare QWidget draws no background of its own, so without this the
        # stylesheet's fill and corner radius are silently ignored.
        enable_styled_background(self)
        self.setFixedSize(DOT_SIZE, DOT_SIZE)
        self.set_severity(severity)

    def set_severity(self, severity: StatusSeverity) -> None:
        """Recolour the dot."""
        set_property(self, "severity", severity.value)

    @property
    def severity(self) -> StatusSeverity:
        """The severity currently in force."""
        return StatusSeverity(str(self.property("severity")))


class StatusLabel(QWidget):
    """A :class:`StatusDot` followed by the text that says what it means."""

    def __init__(
        self,
        text: str = "",
        severity: StatusSeverity = StatusSeverity.NEUTRAL,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self._dot = StatusDot(severity, self)
        self._label = QLabel(text, self)
        row.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._label, 1)
        self.set_status(text, severity)

    def set_status(self, text: str, severity: StatusSeverity) -> None:
        """Set both halves at once; they are never allowed to disagree."""
        shown = text or severity.label
        self._label.setText(shown)
        self._dot.set_severity(severity)
        self.setAccessibleName(shown)

    def text(self) -> str:
        """The visible text."""
        return self._label.text()
