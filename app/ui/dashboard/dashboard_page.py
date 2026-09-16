"""The dashboard.

Answers four questions in the order a user actually asks them:

1. Is the app able to do anything at all? (Amazon connected, browser ready)
2. Is anything waiting on me?
3. What is it watching?
4. What has it been doing?

Everything else belongs on another screen. The temptation with a dashboard is
to show every number available; that produces a wall of figures nobody reads,
so this one shows four tiles and a short activity list, and the tiles are
clickable shortcuts to the screen that can act on them.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.config import SettingsService
from app.core.timeutil import format_clock, format_relative, from_iso
from app.database.records import ActivityEvent, ActivitySeverity
from app.database.repositories import ActivityRepository, OrderRepository, WatchRepository
from app.ui.components import (
    ButtonRow,
    Card,
    EmptyState,
    MetricTile,
    PrimaryButton,
    StatusBadge,
    StatusLabel,
    SubtleButton,
    clear_layout,
)
from app.ui.components.badge import severity_for_activity
from app.ui.theme import StatusSeverity, font_body, font_title

logger = logging.getLogger("app.ui.dashboard")

#: How many activity entries the dashboard shows before pointing at the
#: Activity screen. Short on purpose: this is a summary, not a log viewer.
RECENT_ACTIVITY_LIMIT = 6


class DashboardPage(QWidget):
    """The summary screen."""

    new_purchase_requested = Signal()
    add_watch_requested = Signal()
    open_amazon_requested = Signal()
    connect_amazon_requested = Signal()
    toggle_monitoring_requested = Signal()
    show_watchlist_requested = Signal()
    show_activity_requested = Signal()
    show_attention_requested = Signal()

    def __init__(
        self,
        *,
        watches: WatchRepository,
        activity: ActivityRepository,
        orders: OrderRepository,
        settings: SettingsService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._watches = watches
        self._activity = activity
        self._orders = orders
        self._settings = settings

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setObjectName("ScrollAreaFlat")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(16)

        layout.addWidget(self._build_header())
        layout.addWidget(self._build_account_card())
        layout.addWidget(self._build_tiles())
        layout.addWidget(self._build_actions())
        # No stretch factor: inside a scroll area a stretched child gets
        # compressed below its minimum height, which makes the activity rows
        # overlap each other. The scroll area handles the overflow instead.
        layout.addWidget(self._build_activity_card())
        layout.addStretch(1)

        scroll.setWidget(body)
        root.addWidget(scroll)

        settings.changed.connect(lambda _s: self.refresh())

    # ---- construction ----------------------------------------------------

    def _build_header(self) -> QWidget:
        header = QWidget()
        layout = QHBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 0)

        text = QWidget()
        text_layout = QVBoxLayout(text)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)

        title = QLabel("Overview")
        title.setFont(font_title())
        title.setProperty("role", "title")
        text_layout.addWidget(title)

        self._subtitle = QLabel()
        self._subtitle.setProperty("role", "subtitle")
        text_layout.addWidget(self._subtitle)
        layout.addWidget(text, 1)

        self._test_mode_badge = StatusBadge()
        layout.addWidget(
            self._test_mode_badge, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
        )
        return header

    def _build_account_card(self) -> Card:
        self._account_card = Card(title="Amazon account", icon="amazon_account")

        self._account_status = StatusLabel("Checking...", StatusSeverity.NEUTRAL)
        self._account_card.add_widget(self._account_status)

        self._account_detail = QLabel()
        self._account_detail.setProperty("role", "caption")
        self._account_detail.setWordWrap(True)
        self._account_card.add_widget(self._account_detail)

        self._connect_button = PrimaryButton("Connect Amazon")
        self._connect_button.clicked.connect(self.connect_amazon_requested)
        self._open_amazon_button = SubtleButton("Open Amazon")
        self._open_amazon_button.clicked.connect(self.open_amazon_requested)
        self._account_card.add_widget(
            ButtonRow(self._connect_button, self._open_amazon_button, align="left")
        )
        return self._account_card

    def _build_tiles(self) -> QWidget:
        container = QWidget()
        grid = QGridLayout(container)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)

        self._watching_tile = MetricTile(clickable=True)
        self._watching_tile.set_caption("Watching")
        self._watching_tile.clicked.connect(self.show_watchlist_requested)
        grid.addWidget(self._watching_tile, 0, 0)

        self._waiting_tile = MetricTile(clickable=True)
        self._waiting_tile.set_caption("Waiting for price")
        self._waiting_tile.clicked.connect(self.show_watchlist_requested)
        grid.addWidget(self._waiting_tile, 0, 1)

        self._attention_tile = MetricTile(clickable=True)
        self._attention_tile.set_caption("Needs attention")
        self._attention_tile.clicked.connect(self.show_attention_requested)
        grid.addWidget(self._attention_tile, 0, 2)

        self._purchases_tile = MetricTile(clickable=True)
        self._purchases_tile.set_caption("Purchased")
        self._purchases_tile.clicked.connect(self.show_activity_requested)
        grid.addWidget(self._purchases_tile, 0, 3)

        for column in range(4):
            grid.setColumnStretch(column, 1)
        return container

    def _build_actions(self) -> Card:
        card = Card()
        new_purchase = PrimaryButton("New purchase")
        new_purchase.clicked.connect(self.new_purchase_requested)

        add_watch = SubtleButton("Add a watch")
        add_watch.clicked.connect(self.add_watch_requested)

        self._monitoring_button = SubtleButton("Pause monitoring")
        self._monitoring_button.clicked.connect(self.toggle_monitoring_requested)

        card.add_widget(
            ButtonRow(
                new_purchase, add_watch, self._monitoring_button, align="left"
            )
        )
        self._monitoring_note = QLabel()
        self._monitoring_note.setProperty("role", "caption")
        self._monitoring_note.setWordWrap(True)
        card.add_widget(self._monitoring_note)
        return card

    def _build_activity_card(self) -> Card:
        self._activity_card = Card(title="Recent activity")
        view_all = SubtleButton("View all")
        view_all.clicked.connect(self.show_activity_requested)
        self._activity_card.set_header_action(view_all)

        self._activity_container = QWidget()
        self._activity_layout = QVBoxLayout(self._activity_container)
        self._activity_layout.setContentsMargins(0, 0, 0, 0)
        self._activity_layout.setSpacing(2)
        self._activity_card.add_widget(self._activity_container)

        self._activity_empty = EmptyState(
            icon="clock",
            heading="Nothing has happened yet",
            body=(
                "Checks, purchases and anything that needs your attention will "
                "appear here."
            ),
        )
        self._activity_card.add_widget(self._activity_empty)
        return self._activity_card

    # ---- refresh ---------------------------------------------------------

    def refresh(self) -> None:
        """Re-read every figure. Cheap enough to call on any change."""
        settings = self._settings.current
        counts = self._watches.counts()

        self._watching_tile.set_value(str(counts.get("watching", 0)))
        self._waiting_tile.set_value(str(counts.get("waiting_for_price", 0)))

        attention = counts.get("needs_attention", 0)
        self._attention_tile.set_value(str(attention))
        self._attention_tile.set_severity(
            StatusSeverity.WARNING if attention else StatusSeverity.NEUTRAL
        )

        orders = self._orders.list_recent(limit=200)
        self._purchases_tile.set_value(str(len(orders)))
        spent = self._orders.total_spent()
        self._purchases_tile.set_caption(
            f"Purchased ({spent.format()})" if spent else "Purchased"
        )

        self._refresh_account(settings)
        self._refresh_test_mode(settings)
        self._refresh_activity()

        total = counts.get("total", 0)
        noun = "product" if total == 1 else "products"
        if total == 0:
            self._subtitle.setText("Nothing is being watched yet.")
        elif attention:
            verb = "needs" if attention == 1 else "need"
            self._subtitle.setText(
                f"{attention} of {total} watched {noun} {verb} your attention."
            )
        else:
            self._subtitle.setText(f"{total} watched {noun}, all fine.")

    def _refresh_account(self, settings: object) -> None:
        connected = bool(getattr(settings, "amazon_connected", False))
        name = getattr(settings, "amazon_account_label", None)
        verified = from_iso(getattr(settings, "amazon_last_verified_at", None))

        if connected:
            self._account_status.set_status("Connected", StatusSeverity.SUCCESS)
            detail = f"Signed in as {name}." if name else "Signed in."
            if verified is not None:
                detail += f" Last confirmed {format_relative(verified)}."
            detail += (
                " Your password is never seen or stored by this app."
            )
            self._account_detail.setText(detail)
            self._connect_button.setText("Reconnect")
            self._open_amazon_button.setEnabled(True)
        else:
            self._account_status.set_status("Not connected", StatusSeverity.WARNING)
            self._account_detail.setText(
                "Connect your Amazon account once so the app can read prices "
                "and use your normal checkout. You sign in directly with "
                "Amazon; this app never sees your password."
            )
            self._connect_button.setText("Connect Amazon")
            self._open_amazon_button.setEnabled(True)

    def _refresh_test_mode(self, settings: object) -> None:
        if getattr(settings, "test_mode", True):
            self._test_mode_badge.set_status("Test mode on", StatusSeverity.INFO)
            self._test_mode_badge.setToolTip(
                "No order can be placed while test mode is on."
            )
        else:
            self._test_mode_badge.set_status(
                "Real orders enabled", StatusSeverity.WARNING
            )
            self._test_mode_badge.setToolTip(
                "Test mode is off, so real orders can be placed."
            )

    def set_monitoring(self, running: bool) -> None:
        """Reflect whether the scheduler is running."""
        self._monitoring_button.setText(
            "Pause monitoring" if running else "Resume monitoring"
        )
        self._monitoring_note.setText(
            "Checks run on a schedule, and keep running when the window is closed."
            if running
            else "Monitoring is paused. Nothing is being checked."
        )

    def _refresh_activity(self) -> None:
        clear_layout(self._activity_layout)

        events = self._activity.recent_for_dashboard(limit=RECENT_ACTIVITY_LIMIT)
        self._activity_empty.setVisible(not events)
        self._activity_container.setVisible(bool(events))

        for event in events:
            self._activity_layout.addWidget(_ActivityRow(event))

    # ---- accessors for tests ---------------------------------------------

    @property
    def activity_row_count(self) -> int:
        return self._activity_layout.count()


class _ActivityRow(QWidget):
    """One compact line in the dashboard's activity list."""

    def __init__(self, event: ActivityEvent, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # A floor on the height, so a constrained parent cannot squeeze two
        # rows into the same pixels.
        self.setMinimumHeight(40 if event.detail else 26)
        self.setSizePolicy(
            self.sizePolicy().horizontalPolicy(),
            self.sizePolicy().Policy.Minimum,
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(10)

        time_label = QLabel(format_clock(event.created_at))
        time_label.setProperty("role", "caption")
        time_label.setFixedWidth(64)
        layout.addWidget(time_label)

        from app.ui.components import StatusDot

        dot = StatusDot()
        dot.set_severity(severity_for_activity(event.severity))
        layout.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)

        text = QWidget()
        text_layout = QVBoxLayout(text)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(0)

        title = QLabel(event.title)
        title.setFont(font_body())
        title.setWordWrap(False)
        text_layout.addWidget(title)

        if event.detail:
            detail = QLabel(event.detail)
            detail.setProperty("role", "caption")
            text_layout.addWidget(detail)
        layout.addWidget(text, 1)

        if event.amount is not None:
            amount = QLabel(event.amount.format())
            amount.setFont(font_body())
            amount.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            layout.addWidget(amount)

        accessible = f"{format_clock(event.created_at)}, {event.title}"
        if event.severity is not ActivitySeverity.INFO:
            accessible = f"{accessible}, {event.severity.value}"
        self.setAccessibleName(accessible)
