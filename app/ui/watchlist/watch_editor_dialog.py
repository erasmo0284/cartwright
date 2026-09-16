"""Editing a watch: its rules, its schedule and what it does when triggered.

Switching a watch from "tell me" to "buy it" is the single most consequential
change a user can make in this application, so it is not a toggle they can
brush past:

* The choice is presented as two labelled options with their consequences
  spelled out, not a checkbox.
* Choosing automatic purchasing requires the one-time consent to have been
  given already; if it has not, the option is disabled and says why.
* When automatic purchasing is selected, the dialog restates the limits that
  will be enforced, using the values actually in the form, so the user sees
  the numbers that will govern a purchase made while they are asleep.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.branding import BRAND
from app.config import CHECK_INTERVAL_CHOICES, SettingsService
from app.core.timeutil import format_duration, to_iso, utcnow
from app.database.records import WatchAction, WatchJobView
from app.purchasing.models import ProductSnapshot, PurchaseRules
from app.ui.components import (
    ButtonRow,
    Card,
    PrimaryButton,
    StatusBadge,
    SubtleButton,
)
from app.ui.purchase.rules_editor import RulesEditor
from app.ui.theme import StatusSeverity, font_body, font_title

logger = logging.getLogger("app.ui.watchlist.editor")


class WatchEditorDialog(QDialog):
    """Create or change a watch."""

    #: Emitted when the user asks to turn on automatic purchasing and has not
    #: yet given consent. The main window shows the consent dialog.
    auto_buy_consent_requested = Signal()

    def __init__(
        self,
        *,
        settings: SettingsService,
        rules: PurchaseRules,
        snapshot: ProductSnapshot | None = None,
        product_title: str,
        action: WatchAction = WatchAction.NOTIFY,
        interval_seconds: int | None = None,
        expires_at: str | None = None,
        trigger_in_stock: bool = True,
        trigger_target_price: bool = True,
        is_new: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._is_new = is_new

        self.setWindowTitle(
            f"{'Add a watch' if is_new else 'Edit watch'} - {BRAND.display_name}"
        )
        self.setModal(True)
        self.setMinimumSize(560, 640)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(20, 18, 20, 8)
        header_layout.setSpacing(4)
        heading = QLabel("Add a watch" if is_new else "Edit watch")
        heading.setFont(font_title())
        heading.setProperty("role", "title")
        header_layout.addWidget(heading)
        subtitle = QLabel(product_title)
        subtitle.setProperty("role", "subtitle")
        subtitle.setWordWrap(True)
        header_layout.addWidget(subtitle)
        outer.addWidget(header)

        scroll = QScrollArea()
        scroll.setObjectName("ScrollAreaFlat")
        scroll.setWidgetResizable(True)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(20, 8, 20, 16)
        body_layout.setSpacing(14)

        body_layout.addWidget(self._build_trigger_card(trigger_in_stock, trigger_target_price))

        self._rules_editor = RulesEditor()
        self._rules_editor.set_rules(rules, snapshot=snapshot)
        self._rules_editor.rules_changed.connect(lambda _r: self._refresh_auto_summary())
        body_layout.addWidget(self._rules_editor)

        body_layout.addWidget(
            self._build_schedule_card(interval_seconds, expires_at)
        )
        body_layout.addWidget(self._build_action_card(action))
        body_layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        footer = QWidget()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(20, 12, 20, 16)
        cancel = SubtleButton("Cancel")
        cancel.setDefault(True)
        cancel.clicked.connect(self.reject)
        save = PrimaryButton("Start watching" if is_new else "Save changes")
        save.setAutoDefault(False)
        save.clicked.connect(self._on_save)
        footer_layout.addStretch(1)
        footer_layout.addWidget(ButtonRow(cancel, save, align="right"))
        outer.addWidget(footer)

        self._refresh_auto_summary()

    # ---- construction ----------------------------------------------------

    def _build_trigger_card(self, in_stock: bool, target_price: bool) -> Card:
        from PySide6.QtWidgets import QCheckBox

        card = Card(
            title="Buy or tell me when",
            subtitle="Both conditions must hold before anything happens.",
        )
        self._trigger_in_stock = QCheckBox("It is in stock")
        self._trigger_in_stock.setChecked(in_stock)
        card.add_widget(self._trigger_in_stock)

        self._trigger_target_price = QCheckBox(
            "The price is at or below my maximum item price"
        )
        self._trigger_target_price.setChecked(target_price)
        card.add_widget(self._trigger_target_price)

        note = QLabel(
            "Turning a condition off only stops it from starting a purchase. "
            "Your price limits are always enforced."
        )
        note.setProperty("role", "caption")
        note.setWordWrap(True)
        card.add_widget(note)
        return card

    def _build_schedule_card(
        self, interval_seconds: int | None, expires_at: str | None
    ) -> Card:
        card = Card(title="How often to check")

        row = QHBoxLayout()
        row.setSpacing(8)
        self._interval = QComboBox()
        self._interval.setAccessibleName("Check interval")
        for seconds in CHECK_INTERVAL_CHOICES:
            self._interval.addItem(format_duration(seconds), seconds)
        wanted = interval_seconds or self._settings.current.default_check_interval_seconds
        index = self._interval.findData(wanted)
        self._interval.setCurrentIndex(index if index >= 0 else 1)
        row.addWidget(self._interval)
        row.addStretch(1)
        card.add_layout(row)

        pace_note = QLabel(
            "Checks are spaced out and given a little randomness so Amazon is "
            "never hit with a regular burst of requests."
        )
        pace_note.setProperty("role", "caption")
        pace_note.setWordWrap(True)
        card.add_widget(pace_note)

        stop_row = QHBoxLayout()
        stop_row.setSpacing(8)
        self._never_expires = QRadioButton("Keep watching until I stop it")
        self._until_date = QRadioButton("Stop watching on")
        self._never_expires.setChecked(expires_at is None)
        self._until_date.setChecked(expires_at is not None)

        self._expiry = QDateEdit()
        self._expiry.setCalendarPopup(True)
        self._expiry.setAccessibleName("Stop watching on")
        self._expiry.setMinimumDate(QDate.currentDate().addDays(1))
        self._expiry.setDate(QDate.currentDate().addDays(30))
        self._expiry.setEnabled(expires_at is not None)
        self._until_date.toggled.connect(self._expiry.setEnabled)

        card.add_widget(self._never_expires)
        stop_row.addWidget(self._until_date)
        stop_row.addWidget(self._expiry)
        stop_row.addStretch(1)
        card.add_layout(stop_row)
        return card

    def _build_action_card(self, action: WatchAction) -> Card:
        card = Card(title="When the conditions are met")

        self._notify_option = QRadioButton(WatchAction.NOTIFY.label)
        self._notify_option.setChecked(action is WatchAction.NOTIFY)
        card.add_widget(self._notify_option)
        notify_note = QLabel(
            "The app tells you and waits. You decide whether to buy."
        )
        notify_note.setProperty("role", "caption")
        notify_note.setWordWrap(True)
        card.add_widget(notify_note)

        from app.ui.components import HLine

        card.add_widget(HLine())

        self._buy_option = QRadioButton(WatchAction.BUY.label)
        self._buy_option.setChecked(action is WatchAction.BUY)
        self._buy_option.toggled.connect(self._on_buy_toggled)
        card.add_widget(self._buy_option)

        self._auto_badge = StatusBadge()
        card.add_widget(self._auto_badge)

        self._auto_summary = QLabel()
        self._auto_summary.setProperty("role", "caption")
        self._auto_summary.setWordWrap(True)
        card.add_widget(self._auto_summary)

        self._consent_button = SubtleButton("Turn on automatic purchasing")
        self._consent_button.clicked.connect(self.auto_buy_consent_requested)
        card.add_widget(self._consent_button)

        self._apply_consent_state()
        return card

    # ---- automatic purchasing --------------------------------------------

    def _apply_consent_state(self) -> None:
        acknowledged = self._settings.current.auto_buy_acknowledged
        self._buy_option.setEnabled(acknowledged)
        self._consent_button.setVisible(not acknowledged)
        if not acknowledged:
            self._buy_option.setToolTip(
                "Automatic purchasing has to be switched on once before it can "
                "be used."
            )
            if self._buy_option.isChecked():
                self._notify_option.setChecked(True)
        else:
            self._buy_option.setToolTip("")
        self._refresh_auto_summary()

    def set_auto_buy_acknowledged(self, acknowledged: bool) -> None:
        """Called after the consent dialog, to re-enable the option."""
        self._apply_consent_state()
        if acknowledged:
            self._buy_option.setChecked(True)

    def _on_buy_toggled(self, checked: bool) -> None:
        self._refresh_auto_summary()
        if checked:
            logger.info("Automatic purchasing selected for a watch")

    def _refresh_auto_summary(self) -> None:
        if not hasattr(self, "_auto_summary"):
            return
        buying = self._buy_option.isChecked()
        rules = self._rules_editor.rules()
        test_mode = self._settings.current.test_mode

        if not buying:
            self._auto_badge.setVisible(False)
            self._auto_summary.setText(
                "The app will order for you without asking each time. Every "
                "check below still has to pass."
            )
            return

        self._auto_badge.setVisible(True)
        if test_mode:
            self._auto_badge.set_status(
                "Test mode is on, so nothing will be ordered", StatusSeverity.INFO
            )
        else:
            self._auto_badge.set_status(
                "Real orders will be placed automatically", StatusSeverity.WARNING
            )

        item = rules.max_item_price.format() if rules.max_item_price else "no limit set"
        total = (
            rules.max_order_total.format() if rules.max_order_total else "no limit set"
        )
        self._auto_summary.setText(
            f"It will order {rules.quantity} at up to {item} per item, with an "
            f"order total of at most {total}, only from "
            f"{rules.describe_seller_rule().lower()}, only "
            f"{rules.condition_policy.label.lower()}, and only the exact "
            "version you chose. Anything else stops the order."
        )

    # ---- results ---------------------------------------------------------

    def _on_save(self) -> None:
        if not self._rules_editor.is_valid():
            return
        if self._buy_option.isChecked() and not self._settings.current.auto_buy_acknowledged:
            # Belt and braces: the option is disabled without consent, but a
            # purchase decision must never depend on a widget's enabled state.
            self._notify_option.setChecked(True)
            return
        self.accept()

    def rules(self) -> PurchaseRules:
        return self._rules_editor.rules()

    def action(self) -> WatchAction:
        return WatchAction.BUY if self._buy_option.isChecked() else WatchAction.NOTIFY

    def interval_seconds(self) -> int:
        return int(self._interval.currentData() or CHECK_INTERVAL_CHOICES[1])

    def trigger_in_stock(self) -> bool:
        return self._trigger_in_stock.isChecked()

    def trigger_target_price(self) -> bool:
        return self._trigger_target_price.isChecked()

    def expires_at(self) -> str | None:
        if not self._until_date.isChecked():
            return None
        date = self._expiry.date().toPython()
        # Stored as end-of-day UTC so "stop on the 30th" includes the 30th.
        moment = utcnow().replace(
            year=date.year, month=date.month, day=date.day,
            hour=23, minute=59, second=59, microsecond=0,
        )
        return to_iso(moment)

    @classmethod
    def for_existing(
        cls,
        view: WatchJobView,
        settings: SettingsService,
        parent: QWidget | None = None,
    ) -> WatchEditorDialog:
        """Build an editor pre-filled from a stored watch."""
        return cls(
            settings=settings,
            rules=view.rules,
            snapshot=None,
            product_title=view.product.display_title,
            action=view.job.action,
            interval_seconds=view.job.interval_seconds,
            expires_at=to_iso(view.job.expires_at) if view.job.expires_at else None,
            trigger_in_stock=view.job.trigger_in_stock,
            trigger_target_price=view.job.trigger_target_price,
            is_new=False,
            parent=parent,
        )
