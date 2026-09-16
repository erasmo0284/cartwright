"""The New Purchase screen.

The whole screen is one linear flow, which is why it reads as a single
column: paste a link, see what the app found, set the limits, choose what
happens. Each stage only appears once the previous one has succeeded, so
there is never a form on screen asking for limits on a product that could not
be read.

Three deliberate choices:

* **The paste field is the only thing on screen at first.** A nontechnical
  user should not have to work out where to begin.
* **"Buy with confirmation" is the default action and is visually primary.**
  Automatic purchasing is on the Watch tab, behind its own consent, and is
  never a one-click alternative here.
* **Test mode is stated on this screen**, not just in Settings, because it
  changes what the buttons will actually do.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.config import CHECK_INTERVAL_CHOICES, SettingsService
from app.core.errors import AppError
from app.core.timeutil import format_duration
from app.database.records import WatchAction
from app.purchasing.models import ProductSnapshot, PurchaseRules
from app.purchasing.product_service import Inspection
from app.ui.components import (
    ButtonRow,
    Card,
    EmptyState,
    PrimaryButton,
    ProductSummaryCard,
    ProgressStrip,
    StatusBadge,
    SubtleButton,
)
from app.ui.purchase.rules_editor import RulesEditor
from app.ui.theme import StatusSeverity, font_body, font_title

logger = logging.getLogger("app.ui.purchase.page")


class NewPurchasePage(QWidget):
    """Paste a link, inspect it, set rules, then buy or watch."""

    #: rules, and whether the user allowed the cart to be set aside
    prepare_requested = Signal(object, bool)
    #: rules, action, interval seconds
    watch_requested = Signal(object, object, int)
    inspect_requested = Signal(str)
    cancel_requested = Signal()
    open_product_requested = Signal(str)

    def __init__(
        self, settings: SettingsService, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._inspection: Inspection | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setObjectName("ScrollAreaFlat")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        body = QWidget()
        self._layout = QVBoxLayout(body)
        self._layout.setContentsMargins(24, 20, 24, 24)
        self._layout.setSpacing(16)

        self._layout.addWidget(self._build_header())
        self._layout.addWidget(self._build_paste_card())

        self._progress = ProgressStrip()
        self._progress.cancel_requested.connect(self.cancel_requested)
        self._layout.addWidget(self._progress)

        self._empty = EmptyState(
            icon="cart",
            heading="Nothing to buy yet",
            body=(
                "Paste an Amazon product link above and the app will read the "
                "price, the seller and the delivery details before anything is "
                "ordered."
            ),
        )
        self._layout.addWidget(self._empty)

        self._result_area = QWidget()
        result_layout = QVBoxLayout(self._result_area)
        result_layout.setContentsMargins(0, 0, 0, 0)
        result_layout.setSpacing(16)

        self._product_card = ProductSummaryCard()
        result_layout.addWidget(self._product_card)

        self._rules_editor = RulesEditor()
        result_layout.addWidget(self._rules_editor)

        result_layout.addWidget(self._build_action_card())
        self._result_area.setVisible(False)
        self._layout.addWidget(self._result_area)

        self._layout.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll)

        settings.changed.connect(lambda _s: self._refresh_mode_note())
        self._refresh_mode_note()

    # ---- construction ----------------------------------------------------

    def _build_header(self) -> QWidget:
        header = QWidget()
        layout = QVBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        title = QLabel("New purchase")
        title.setFont(font_title())
        title.setProperty("role", "title")
        layout.addWidget(title)

        subtitle = QLabel(
            "Check a product, set your limits, then buy it or watch it."
        )
        subtitle.setProperty("role", "subtitle")
        layout.addWidget(subtitle)
        return header

    def _build_paste_card(self) -> Card:
        from PySide6.QtWidgets import QLineEdit

        card = Card()
        row = QHBoxLayout()
        row.setSpacing(10)

        self._url_input = QLineEdit()
        self._url_input.setObjectName("UrlInput")
        self._url_input.setPlaceholderText(
            "Paste an Amazon product link, or type its 10-character item code"
        )
        self._url_input.setAccessibleName("Amazon product link")
        self._url_input.setClearButtonEnabled(True)
        self._url_input.returnPressed.connect(self._on_check)
        self._url_input.textChanged.connect(self._on_url_changed)
        row.addWidget(self._url_input, 1)

        self._check_button = PrimaryButton("Check product")
        self._check_button.setEnabled(False)
        self._check_button.clicked.connect(self._on_check)
        row.addWidget(self._check_button)

        card.add_layout(row)

        self._input_hint = QLabel(
            "Nothing is bought by checking a product. This only reads the page."
        )
        self._input_hint.setObjectName("InlineHint")
        self._input_hint.setProperty("role", "caption")
        self._input_hint.setProperty("severity", StatusSeverity.NEUTRAL.value)
        self._input_hint.setWordWrap(True)
        card.add_widget(self._input_hint)
        return card

    def _build_action_card(self) -> Card:
        card = Card(title="What should happen")

        self._mode_note = StatusBadge()
        card.set_header_action(self._mode_note)

        self._rule_warning = QLabel()
        self._rule_warning.setProperty("role", "caption")
        self._rule_warning.setWordWrap(True)
        self._rule_warning.setVisible(False)
        card.add_widget(self._rule_warning)
        self._rules_editor.warning_changed.connect(self._on_rule_warning)

        buy_row = QWidget()
        buy_layout = QVBoxLayout(buy_row)
        buy_layout.setContentsMargins(0, 0, 0, 0)
        buy_layout.setSpacing(6)

        self._prepare_button = PrimaryButton("Buy with confirmation")
        self._prepare_button.setAccessibleName("Buy with confirmation")
        self._prepare_button.clicked.connect(self._on_prepare)
        buy_layout.addWidget(self._prepare_button)

        explain = QLabel(
            "The app prepares the order, runs every check, and shows you the "
            "exact total before anything is placed."
        )
        explain.setProperty("role", "caption")
        explain.setWordWrap(True)
        buy_layout.addWidget(explain)
        card.add_widget(buy_row)

        from app.ui.components import HLine

        card.add_widget(HLine())

        watch_row = QWidget()
        watch_layout = QVBoxLayout(watch_row)
        watch_layout.setContentsMargins(0, 0, 0, 0)
        watch_layout.setSpacing(6)

        interval_row = QHBoxLayout()
        interval_row.setSpacing(8)
        interval_label = QLabel("Check every")
        interval_label.setProperty("role", "caption")
        interval_row.addWidget(interval_label)

        self._interval = QComboBox()
        self._interval.setAccessibleName("How often to check")
        for seconds in CHECK_INTERVAL_CHOICES:
            self._interval.addItem(format_duration(seconds), seconds)
        interval_row.addWidget(self._interval)
        interval_row.addStretch(1)
        watch_layout.addLayout(interval_row)

        self._watch_button = SubtleButton("Watch and notify me")
        self._watch_button.setAccessibleName("Watch and notify me")
        self._watch_button.clicked.connect(self._on_watch)
        watch_layout.addWidget(self._watch_button)

        watch_explain = QLabel(
            "The app checks the price and stock on this schedule and tells you "
            "when your rules are met. You still decide whether to buy."
        )
        watch_explain.setProperty("role", "caption")
        watch_explain.setWordWrap(True)
        watch_layout.addWidget(watch_explain)

        card.add_widget(watch_row)

        self._open_button = SubtleButton("Open on Amazon")
        self._open_button.clicked.connect(self._on_open_product)
        card.add_widget(ButtonRow(self._open_button, align="left"))
        return card

    # ---- state -----------------------------------------------------------

    def focus_input(self) -> None:
        """Put the cursor in the paste field. Called when the page is shown."""
        self._url_input.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self._url_input.selectAll()

    def reset(self) -> None:
        """Clear the screen back to its starting state."""
        self._inspection = None
        self._url_input.clear()
        self._result_area.setVisible(False)
        self._empty.setVisible(True)
        self._progress.stop()
        self._set_input_hint(
            "Nothing is bought by checking a product. This only reads the page.",
            StatusSeverity.NEUTRAL,
        )

    def prefill(self, text: str) -> None:
        """Put a link in the field, ready to check."""
        self._url_input.setText(text)
        self.focus_input()

    def show_progress(self, message: str) -> None:
        self._progress.start(message)
        self._check_button.setEnabled(False)
        self._empty.setVisible(False)

    def update_progress(self, message: str) -> None:
        self._progress.set_message(message)

    def show_inspection(self, inspection: Inspection) -> None:
        """Render a successful product check and reveal the rules."""
        self._inspection = inspection
        self._progress.stop()
        self._check_button.setEnabled(bool(self._url_input.text().strip()))
        self._empty.setVisible(False)

        self._product_card.set_product(inspection.snapshot)
        self._rules_editor.set_rules(
            inspection.suggested_rules, snapshot=inspection.snapshot
        )
        self._result_area.setVisible(True)
        self._refresh_mode_note()
        self._update_actions_enabled()

        if not inspection.snapshot.in_stock:
            self._set_input_hint(
                "This item is not available to buy right now. You can still "
                "watch it and be told when it returns.",
                StatusSeverity.WARNING,
            )
        else:
            self._set_input_hint(
                "Read from Amazon just now. Prices can change at any time, so "
                "everything is checked again before ordering.",
                StatusSeverity.NEUTRAL,
            )

    def show_failure(self, error: AppError) -> None:
        """Render a failed product check inline, keeping the input intact."""
        self._progress.stop()
        self._result_area.setVisible(False)
        self._empty.setVisible(True)
        self._check_button.setEnabled(bool(self._url_input.text().strip()))
        self._set_input_hint(f"{error.title}. {error.detail}", StatusSeverity.ERROR)

    def show_cancelled(self) -> None:
        self._progress.stop()
        self._check_button.setEnabled(bool(self._url_input.text().strip()))
        self._set_input_hint("Stopped. Nothing was ordered.", StatusSeverity.NEUTRAL)

    # ---- reactions -------------------------------------------------------

    def _on_url_changed(self, text: str) -> None:
        self._check_button.setEnabled(bool(text.strip()))

    def _on_check(self) -> None:
        text = self._url_input.text().strip()
        if not text:
            return
        self.inspect_requested.emit(text)

    def _on_prepare(self) -> None:
        if self._inspection is None or not self._rules_editor.is_valid():
            return
        rules = self._rules_editor.rules()
        logger.info(
            "Prepare purchase requested",
            extra={
                "asin": rules.expected_asin,
                "quantity": rules.quantity,
                "test_mode": self._settings.current.test_mode,
            },
        )
        # Permission to set the cart aside is asked for later, and only if it
        # turns out to be needed, so it is not requested speculatively here.
        self.prepare_requested.emit(rules, False)

    def _on_watch(self) -> None:
        if self._inspection is None or not self._rules_editor.is_valid():
            return
        rules = self._rules_editor.rules()
        interval = int(self._interval.currentData() or CHECK_INTERVAL_CHOICES[1])
        self.watch_requested.emit(rules, WatchAction.NOTIFY, interval)

    def _on_open_product(self) -> None:
        if self._inspection is None:
            return
        self.open_product_requested.emit(self._inspection.record.product_url)

    def _on_rule_warning(self, warning: str) -> None:
        self._rule_warning.setText(warning)
        self._rule_warning.setVisible(bool(warning))
        self._update_actions_enabled()

    def _update_actions_enabled(self) -> None:
        has_product = self._inspection is not None
        valid = self._rules_editor.is_valid()
        in_stock = bool(
            self._inspection and self._inspection.snapshot.in_stock
        )
        self._prepare_button.setEnabled(has_product and valid and in_stock)
        if has_product and valid and not in_stock:
            self._prepare_button.setToolTip(
                "This item is not available to buy right now."
            )
        else:
            self._prepare_button.setToolTip("")
        self._watch_button.setEnabled(has_product and valid)
        self._open_button.setEnabled(has_product)

    def _refresh_mode_note(self) -> None:
        """Say plainly what the buy button will do, given test mode."""
        if not hasattr(self, "_mode_note"):
            return
        if self._settings.current.test_mode:
            self._mode_note.set_status(
                "Test mode: no order will be placed", StatusSeverity.INFO
            )
            self._prepare_button.setText("Run a test")
        else:
            self._mode_note.set_status(
                "Real orders can be placed", StatusSeverity.WARNING
            )
            self._prepare_button.setText("Buy with confirmation")

    def _set_input_hint(self, text: str, severity: StatusSeverity) -> None:
        """Set the line under the input, coloured by what it means.

        The severity is what the stylesheet matches on, so an error under the
        field cannot end up looking like ordinary guidance.
        """
        self._input_hint.setText(text)
        self._input_hint.setProperty("severity", severity.value)
        # Screen readers announce the meaning as well as the words.
        self._input_hint.setAccessibleDescription(
            text if severity is StatusSeverity.NEUTRAL else f"{severity.label}: {text}"
        )
        from app.ui.components.common import repolish

        repolish(self._input_hint)

    # ---- accessors -------------------------------------------------------

    def current_rules(self) -> PurchaseRules | None:
        return self._rules_editor.rules() if self._inspection else None

    def current_snapshot(self) -> ProductSnapshot | None:
        return self._inspection.snapshot if self._inspection else None

    def current_product_id(self) -> int | None:
        return self._inspection.record.id if self._inspection else None
