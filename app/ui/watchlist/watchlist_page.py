"""The watch list.

One card per watched product. The card answers, without the user clicking
anything: what it is, what it costs now, what they are waiting for, whether
it is on track, when it was last checked and when it will be checked next.

The ordering is deliberate rather than alphabetical or chronological:
anything that needs attention floats to the top, because a watch that has
silently stopped working is worse than one that is simply waiting.

An automatic watch is labelled as such on its card, every time. A user should
never have to open a dialog to find out whether something is going to spend
their money.
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

from app.core.money import Money
from app.core.timeutil import format_relative
from app.database.records import WatchAction, WatchJobView, WatchStatus
from app.database.repositories import ProductRepository, WatchRepository
from app.ui.components import (
    ButtonRow,
    Card,
    EmptyState,
    IconButton,
    PrimaryButton,
    Sparkline,
    StatusBadge,
    SubtleButton,
)
from app.ui.components.badge import severity_for_watch_status
from app.ui.components.common import clear_layout
from app.ui.theme import StatusSeverity, font_body, font_title

logger = logging.getLogger("app.ui.watchlist")


class WatchlistPage(QWidget):
    """Shows every watch, with its controls."""

    add_watch_requested = Signal()
    #: watch_job_id
    check_now_requested = Signal(int)
    pause_requested = Signal(int)
    resume_requested = Signal(int)
    edit_requested = Signal(int)
    remove_requested = Signal(int)
    open_product_requested = Signal(int)
    view_history_requested = Signal(int)

    def __init__(
        self,
        *,
        watches: WatchRepository,
        products: ProductRepository,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._watches = watches
        self._products = products
        self._cards: dict[int, _WatchCard] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        scroll = QScrollArea()
        scroll.setObjectName("ScrollAreaFlat")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._body = QWidget()
        self._list_layout = QVBoxLayout(self._body)
        self._list_layout.setContentsMargins(24, 8, 24, 24)
        self._list_layout.setSpacing(12)

        self._empty = EmptyState(
            icon="watchlist",
            heading="No products being watched",
            body=(
                "Add a product and the app can monitor its price and "
                "availability, then tell you or buy it when your rules are met."
            ),
            action_text="Add product",
        )
        self._empty.action_clicked.connect(self.add_watch_requested)
        self._list_layout.addWidget(self._empty)
        self._list_layout.addStretch(1)

        scroll.setWidget(self._body)
        root.addWidget(scroll, 1)

    def _build_header(self) -> QWidget:
        header = QWidget()
        layout = QHBoxLayout(header)
        layout.setContentsMargins(24, 20, 24, 8)

        text = QWidget()
        text_layout = QVBoxLayout(text)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)
        title = QLabel("Watch list")
        title.setFont(font_title())
        title.setProperty("role", "title")
        text_layout.addWidget(title)
        self._subtitle = QLabel()
        self._subtitle.setProperty("role", "subtitle")
        text_layout.addWidget(self._subtitle)
        layout.addWidget(text, 1)

        self._check_all = SubtleButton("Check all now")
        self._check_all.clicked.connect(lambda: self.check_now_requested.emit(0))
        layout.addWidget(self._check_all)

        add = PrimaryButton("Add product")
        add.clicked.connect(self.add_watch_requested)
        layout.addWidget(add)
        return header

    # ---- refresh ---------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild the list from the database."""
        views = self._watches.list_views()

        # Remove cards whose watch has gone.
        for job_id in list(self._cards):
            if not any(view.job.id == job_id for view in views):
                card = self._cards.pop(job_id)
                self._list_layout.removeWidget(card)
                # Unparent before scheduling deletion, or the removed card
                # keeps painting at its old position until the event loop runs.
                card.setParent(None)
                card.deleteLater()

        for position, view in enumerate(views):
            card = self._cards.get(view.job.id)
            if card is None:
                card = _WatchCard(view)
                card.check_now_requested.connect(self.check_now_requested)
                card.pause_requested.connect(self.pause_requested)
                card.resume_requested.connect(self.resume_requested)
                card.edit_requested.connect(self.edit_requested)
                card.remove_requested.connect(self.remove_requested)
                card.open_product_requested.connect(self.open_product_requested)
                card.view_history_requested.connect(self.view_history_requested)
                self._cards[view.job.id] = card
            card.update_view(view, self._products.price_series(view.product.id))
            self._list_layout.insertWidget(position, card)

        has_any = bool(views)
        self._empty.setVisible(not has_any)
        self._check_all.setEnabled(has_any)

        needing = sum(1 for view in views if view.job.status.needs_attention)
        if not views:
            self._subtitle.setText("Nothing is being watched yet.")
        elif needing:
            self._subtitle.setText(
                f"{len(views)} watched, {needing} needing attention."
            )
        else:
            self._subtitle.setText(
                f"{len(views)} watched. Checks continue while the window is closed."
            )

    def set_progress(self, watch_job_id: int, message: str) -> None:
        card = self._cards.get(watch_job_id)
        if card is not None:
            card.set_progress(message)

    def clear_progress(self, watch_job_id: int) -> None:
        card = self._cards.get(watch_job_id)
        if card is not None:
            card.clear_progress()

    @property
    def card_count(self) -> int:
        return len(self._cards)


class _WatchCard(Card):
    """One watched product."""

    check_now_requested = Signal(int)
    pause_requested = Signal(int)
    resume_requested = Signal(int)
    edit_requested = Signal(int)
    remove_requested = Signal(int)
    open_product_requested = Signal(int)
    view_history_requested = Signal(int)

    def __init__(self, view: WatchJobView, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self._job_id = view.job.id

        # The status badge sits in the body rather than the card header:
        # a header exists only to hold a title, and asking for one here just
        # to carry a badge leaves an empty strip and a stray rule above every
        # card.
        self._status_badge = StatusBadge()

        top = QHBoxLayout()
        top.setSpacing(12)

        self._image = QLabel()
        self._image.setFixedSize(72, 72)
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image.setProperty("role", "caption")
        self._image.setText("No image")
        top.addWidget(self._image, 0, Qt.AlignmentFlag.AlignTop)

        details = QWidget()
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(4)

        self._title = QLabel()
        self._title.setFont(font_body())
        self._title.setWordWrap(True)
        details_layout.addWidget(self._title)

        self._auto_badge = StatusBadge()
        self._auto_badge.setVisible(False)
        details_layout.addWidget(self._auto_badge, 0, Qt.AlignmentFlag.AlignLeft)

        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(18)
        self._grid.setVerticalSpacing(2)
        details_layout.addLayout(self._grid)

        top.addWidget(details, 1)

        # Status and price trend share the right-hand column, so the eye can
        # take in "what state is this in" and "which way is the price going"
        # in one place.
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)
        right_layout.addWidget(
            self._status_badge, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
        )
        self._sparkline = Sparkline()
        self._sparkline.setFixedWidth(140)
        right_layout.addWidget(self._sparkline)
        right_layout.addStretch(1)
        right.setFixedWidth(150)
        top.addWidget(right, 0, Qt.AlignmentFlag.AlignTop)

        self.add_layout(top)

        self._progress = QLabel()
        self._progress.setProperty("role", "caption")
        self._progress.setVisible(False)
        self.add_widget(self._progress)

        self._pause_button = SubtleButton("Pause")
        self._pause_button.clicked.connect(self._on_pause_or_resume)

        check = SubtleButton("Check now")
        check.clicked.connect(lambda: self.check_now_requested.emit(self._job_id))

        edit = SubtleButton("Edit")
        edit.clicked.connect(lambda: self.edit_requested.emit(self._job_id))

        history = SubtleButton("History")
        history.clicked.connect(lambda: self.view_history_requested.emit(self._job_id))

        open_amazon = IconButton("external", "Open on Amazon")
        open_amazon.clicked.connect(
            lambda: self.open_product_requested.emit(self._job_id)
        )

        remove = IconButton("trash", "Stop watching and remove")
        remove.clicked.connect(lambda: self.remove_requested.emit(self._job_id))

        self.add_widget(
            ButtonRow(
                check,
                self._pause_button,
                edit,
                history,
                open_amazon,
                remove,
                align="left",
            )
        )

        self._paused = False
        self.update_view(view, ())

    # ---- population ------------------------------------------------------

    def update_view(
        self, view: WatchJobView, series: "tuple[object, ...] | list[object]"
    ) -> None:
        job = view.job
        self._paused = job.status is WatchStatus.PAUSED

        self._title.setText(view.product.display_title)

        self._status_badge.set_status(
            job.status.label, severity_for_watch_status(job.status)
        )

        if job.action is WatchAction.BUY:
            self._auto_badge.setVisible(True)
            self._auto_badge.set_status(
                "Buys automatically", StatusSeverity.WARNING
            )
            self._auto_badge.setToolTip(
                "When your rules are met this will place a real order without "
                "asking, unless test mode is on."
            )
        else:
            self._auto_badge.setVisible(False)

        self._pause_button.setText("Resume" if self._paused else "Pause")

        self._fill_grid(view)
        self._fill_sparkline(view, series)

        summary = job.last_check_summary or "Not checked yet"
        self.setAccessibleName(
            f"{view.product.display_title}, {job.status.label}, {summary}"
        )

    def _fill_grid(self, view: WatchJobView) -> None:
        clear_layout(self._grid)

        job = view.job
        rows: list[tuple[str, str]] = [
            ("Now", self._money(view.current_price)),
            ("Target", self._money(view.target_price)),
            (
                "Availability",
                view.latest.availability.label if view.latest else "Not checked",
            ),
            ("Seller", (view.latest.seller if view.latest else None) or "Not shown"),
            ("Last checked", format_relative(job.last_checked_at)),
            (
                "Next check",
                "Paused"
                if self._paused
                else format_relative(job.next_check_at),
            ),
        ]
        if job.expires_at is not None:
            rows.append(("Stops", format_relative(job.expires_at)))
        if job.consecutive_failures:
            rows.append(
                (
                    "Failed checks",
                    f"{job.consecutive_failures} in a row; trying less often",
                )
            )

        for index, (label, value) in enumerate(rows):
            column = (index % 3) * 2
            row = index // 3
            key = QLabel(label)
            key.setProperty("role", "caption")
            self._grid.addWidget(key, row, column)
            shown = QLabel(value)
            shown.setFont(font_body())
            shown.setWordWrap(False)
            self._grid.addWidget(shown, row, column + 1)

        if job.last_check_summary:
            summary = QLabel(job.last_check_summary)
            summary.setProperty("role", "caption")
            summary.setWordWrap(True)
            self._grid.addWidget(summary, (len(rows) // 3) + 1, 0, 1, 6)

    def _fill_sparkline(
        self, view: WatchJobView, series: "tuple[object, ...] | list[object]"
    ) -> None:
        prices: list[Money] = [
            observation.price  # type: ignore[union-attr]
            for observation in series
            if getattr(observation, "price", None) is not None
        ]
        self._sparkline.set_series(prices)
        self._sparkline.set_target(view.target_price)
        self._sparkline.setVisible(len(prices) > 1)

    @staticmethod
    def _money(value: Money | None) -> str:
        return value.format() if value is not None else "Not set"

    # ---- progress --------------------------------------------------------

    def set_progress(self, message: str) -> None:
        self._progress.setText(message)
        self._progress.setVisible(True)

    def clear_progress(self) -> None:
        self._progress.setVisible(False)

    def _on_pause_or_resume(self) -> None:
        if self._paused:
            self.resume_requested.emit(self._job_id)
        else:
            self.pause_requested.emit(self._job_id)
