"""The Activity screen.

This is the application's account of itself: what it checked, what it found,
what it bought and what stopped it. It is deliberately a *log* and not a card
gallery -- a user who comes here is scanning for one line, usually the most
recent one -- so the rows are compact, ordered newest first and grouped under
a day heading.

Three decisions worth recording:

* **Paging, not everything.** The feed is capped at five thousand rows in the
  database, and building five thousand row widgets would stall the window for
  seconds. One hundred rows are built at a time, behind a "Show more" button.
* **No error codes.** A stored ``error_code`` is an internal identifier. Where
  a row would otherwise have nothing to say, the code is translated through
  :func:`app.core.errors.describe` into the sentence written for people, and
  an unrecognised code simply contributes nothing.
* **Clearing is history-only.** The confirmation says so in those words,
  because "clear" next to a list of purchases could reasonably be read as
  cancelling them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.errors import ErrorCode, describe
from app.core.timeutil import format_clock, format_day
from app.database.records import ActivityCategory, ActivityEvent
from app.ui.components import (
    Card,
    DangerButton,
    EmptyState,
    HLine,
    StatusDot,
    SubtleButton,
    role_label,
    severity_for_activity,
)
from app.ui.dialogs import confirm_destructive

if TYPE_CHECKING:  # The composition root reaches the automation layer; a
    # screen only needs its shape, so it is not imported at run time.
    from app.ui.app_context import AppContext

logger = logging.getLogger("app.ui.activity")

#: Rows built per "Show more". Chosen so the first page is instant and a user
#: who genuinely wants to read back a month can still get there.
PAGE_SIZE: Final = 100

#: The filter tabs, in the order they are shown. ``None`` is "All". The
#: system category has no tab of its own on purpose: it carries start-up and
#: housekeeping lines that are worth having in the full list but are not
#: something a user goes looking for.
FILTERS: Final[tuple[tuple[str, ActivityCategory | None], ...]] = (
    ("All", None),
    (ActivityCategory.PURCHASE.label, ActivityCategory.PURCHASE),
    (ActivityCategory.WATCH_CHECK.label, ActivityCategory.WATCH_CHECK),
    (ActivityCategory.WARNING.label, ActivityCategory.WARNING),
    (ActivityCategory.ERROR.label, ActivityCategory.ERROR),
    (ActivityCategory.ACCOUNT.label, ActivityCategory.ACCOUNT),
)

#: Per-filter empty state: an icon, a heading and a sentence saying when lines
#: appear. Written per filter because "No activity yet" under the Purchases
#: tab would be wrong -- there may be plenty of activity and no purchases.
_EMPTY_STATES: Final[dict[ActivityCategory | None, tuple[str, str, str]]] = {
    None: (
        "activity",
        "No activity yet",
        "Once you add a product and start watching it, every check, purchase "
        "and problem is listed here.",
    ),
    ActivityCategory.PURCHASE: (
        "cart",
        "No purchases yet",
        "Every purchase the program prepares or completes is listed here, "
        "with the amount.",
    ),
    ActivityCategory.WATCH_CHECK: (
        "watchlist",
        "No checks yet",
        "Each time the program looks at a product's page, the price and "
        "availability it found are listed here.",
    ),
    ActivityCategory.WARNING: (
        "warning",
        "No warnings",
        "Anything the program noticed but carried on through appears here, "
        "such as a seller changing.",
    ),
    ActivityCategory.ERROR: (
        "warning",
        "No errors",
        "If something stops the program from checking a product or "
        "completing a purchase, it is explained here.",
    ),
    ActivityCategory.ACCOUNT: (
        "amazon_account",
        "Nothing about your account yet",
        "Signing in, signing out and anything Amazon asks you to verify are "
        "listed here.",
    ),
}

_CLEAR_TITLE: Final = "Clear the activity history?"
_CLEAR_MESSAGE: Final = (
    "This removes this list only. It does not cancel or change any order "
    "already placed on Amazon, and it leaves your products, watches and "
    "settings exactly as they are."
)

#: Width of the time column. Wide enough for "12:45 PM" at 150% scaling,
#: which is what keeps every row's text starting at the same place.
_TIME_WIDTH: Final = 68




def failure_summary(error_code: str | None) -> str | None:
    """The sentence written for people behind ``error_code``, if any.

    Returns ``None`` for an empty or unrecognised code, so a row falls back to
    having no second line rather than showing an internal identifier.
    """
    if not error_code:
        return None
    try:
        return describe(ErrorCode(error_code)).title
    except (KeyError, ValueError):
        logger.debug("No user-facing text for a stored failure reason")
        return None


class ActivityPage(QWidget):
    """The activity log, filtered by category and grouped by day."""

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._context = context
        self._activity = context.repositories.activity

        self._filter: ActivityCategory | None = None
        self._offset = 0
        self._shown: list[ActivityEvent] = []
        self._last_day: str | None = None
        #: Set when an event arrives while the screen is hidden, so the reload
        #: happens once, when the user actually looks at it.
        self._stale = False
        self._filter_buttons: dict[ActivityCategory | None, QPushButton] = {}

        self._build()
        self.refresh()

    # ---- construction ----------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        root.addLayout(self._build_header())
        root.addWidget(self._build_filter_bar())
        root.addWidget(self._build_feed(), 1)

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(12)

        header.addWidget(role_label("Activity", "title", self))
        header.addStretch(1)

        self.clear_button = DangerButton("Clear history", self)
        self.clear_button.setToolTip(
            "Remove this list. Orders already placed on Amazon are not "
            "affected."
        )
        self.clear_button.clicked.connect(self.clear_history)
        header.addWidget(self.clear_button)
        return header

    def _build_filter_bar(self) -> QWidget:
        bar = QWidget(self)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # An exclusive group of checkable native buttons reads as a segmented
        # control on Windows 11, and a button group gives the arrow-key
        # behaviour of a real tab strip for free.
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)

        for label, category in FILTERS:
            button = QPushButton(label, bar)
            button.setCheckable(True)
            button.setAccessibleName(f"Show {label.lower()}")
            button.setChecked(category is self._filter)
            button.clicked.connect(
                lambda _checked, chosen=category: self.set_filter(chosen)
            )
            self._filter_group.addButton(button)
            self._filter_buttons[category] = button
            layout.addWidget(button)

        layout.addStretch(1)
        return bar

    def _build_feed(self) -> QWidget:
        scroll = QScrollArea(self)
        scroll.setObjectName("ScrollAreaFlat")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setAccessibleName("Activity list")

        content = QWidget()
        outer = QVBoxLayout(content)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        # The rows live in one card so the log reads as a single surface
        # rather than as text floating on the page background.
        self._card = Card(parent=content)
        self._feed = self._card.body_layout
        self._feed.setSpacing(0)
        outer.addWidget(self._card)

        self._empty = EmptyState(parent=content)
        outer.addWidget(self._empty)

        self.more_button = SubtleButton("Show more", content)
        more_row = QHBoxLayout()
        more_row.setContentsMargins(0, 0, 0, 0)
        more_row.addStretch(1)
        more_row.addWidget(self.more_button)
        more_row.addStretch(1)
        self.more_button.clicked.connect(self._load_more)
        outer.addLayout(more_row)

        outer.addStretch(1)
        scroll.setWidget(content)
        self._content = content
        self._apply_empty_text()
        return scroll

    # ---- public API ------------------------------------------------------

    def refresh(self) -> None:
        """Reload the feed from the newest event down."""
        self._stale = False
        self._offset = 0
        self._shown = []
        self._last_day = None
        self._clear_rows()
        self._update_filter_counts()
        self._load_more()

    @Slot()
    def on_activity_added(self) -> None:
        """React to a new event being recorded.

        Reloading only while the screen is visible keeps a busy monitoring run
        from rebuilding a hundred widgets nobody is looking at; the reload
        then happens on the way back in.
        """
        if self.isVisible():
            self.refresh()
        else:
            self._stale = True

    def set_filter(self, category: ActivityCategory | None) -> None:
        """Show only ``category``, or everything when it is ``None``."""
        self._filter = category
        button = self._filter_buttons.get(category)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        self._apply_empty_text()
        self.refresh()

    @property
    def active_filter(self) -> ActivityCategory | None:
        """The category currently shown, or ``None`` for everything."""
        return self._filter

    def visible_events(self) -> tuple[ActivityEvent, ...]:
        """The events that currently have a row, newest first.

        Exposed so the window (and the tests) can ask what the screen is
        showing without reaching into its layout.
        """
        return tuple(self._shown)

    def clear_history(self) -> None:
        """Remove every stored event, after confirming with the user."""
        if not confirm_destructive(
            self,
            title=_CLEAR_TITLE,
            message=_CLEAR_MESSAGE,
            confirm_label="Clear history",
            cancel_label="Keep it",
        ):
            return
        removed = self._activity.clear()
        logger.info("Activity history cleared", extra={"removed": removed})
        self.refresh()

    # ---- Qt overrides ----------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Pick up anything that arrived while the screen was hidden."""
        super().showEvent(event)
        if self._stale:
            self.refresh()

    # ---- loading ---------------------------------------------------------

    def _load_more(self) -> None:
        """Append the next page of rows."""
        events = self._activity.list_events(
            category=self._filter, limit=PAGE_SIZE, offset=self._offset
        )
        self._offset += len(events)
        for event in events:
            day = format_day(event.created_at)
            if day != self._last_day:
                self._feed.addWidget(self._build_day_heading(day))
                self._last_day = day
            self._feed.addWidget(self._build_row(event))
            self._shown.append(event)
        self._update_visibility()

    def _update_visibility(self) -> None:
        total = self._activity.count(category=self._filter)
        has_rows = bool(self._shown)
        self._card.setVisible(has_rows)
        self._empty.setVisible(not has_rows)

        remaining = total - len(self._shown)
        self.more_button.setVisible(remaining > 0)
        if remaining > 0:
            older = "1 older entry" if remaining == 1 else f"{remaining} older entries"
            self.more_button.setText(f"Show more — {older}")

    def _update_filter_counts(self) -> None:
        """Put the number of matching entries on each filter button."""
        for label, category in FILTERS:
            button = self._filter_buttons.get(category)
            if button is None:
                continue
            count = self._activity.count(category=category)
            button.setText(f"{label} ({count})")
            button.setAccessibleName(f"Show {label.lower()}, {count} entries")

    def _apply_empty_text(self) -> None:
        """Retune the empty panel for the filter that is showing."""
        icon, heading, body = _EMPTY_STATES.get(
            self._filter, _EMPTY_STATES[None]
        )
        self._empty.set_icon(icon)
        self._empty.set_text(heading, body)

    def _clear_rows(self) -> None:
        while self._feed.count():
            item = self._feed.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    # ---- row widgets -----------------------------------------------------

    def _build_day_heading(self, day: str) -> QWidget:
        """A day heading with a rule under it, so it reads as pinned."""
        holder = QWidget(self._content)
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 12, 0, 2)
        layout.setSpacing(4)

        label = role_label(day, "sectionHeading", holder)
        label.setAccessibleName(f"Activity on {day}")
        layout.addWidget(label)
        layout.addWidget(HLine(holder))
        return holder

    def _build_row(self, event: ActivityEvent) -> QWidget:
        severity = severity_for_activity(event.severity)

        row = QWidget(self._content)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(10)

        clock = format_clock(event.created_at)
        time_label = role_label(clock, "caption", row)
        time_label.setFixedWidth(_TIME_WIDTH)
        time_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
        )
        layout.addWidget(time_label)

        # A bare dot rather than a StatusLabel: in a log the row's own title
        # already says what happened, so the severity's word beside it would
        # print "Problem" next to a line that reads "Amazon asked you to sign
        # in again". The dot is reinforcement, the words are the signal, and
        # the word still reaches a screen reader through the accessible name.
        marker = StatusDot(severity, row)
        marker.setAccessibleName(severity.label)
        marker.setAccessibleDescription(severity.label)
        layout.addWidget(marker, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(1)
        title = role_label(event.title, None, row)
        title.setWordWrap(True)
        text.addWidget(title)

        detail = event.detail or failure_summary(event.error_code)
        if detail:
            # Wrapped, not elided: a log is read, and half a sentence with an
            # ellipsis is the one thing worse than a long line.
            caption = role_label(detail, "caption", row)
            caption.setWordWrap(True)
            text.addWidget(caption)
        layout.addLayout(text, 1)

        if event.amount is not None:
            amount = role_label(event.amount.format(), "title", row)
            amount.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
            )
            layout.addWidget(amount, 0, Qt.AlignmentFlag.AlignTop)

        row.setAccessibleName(f"{clock}, {severity.label}, {event.title}")
        return row
