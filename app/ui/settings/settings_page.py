"""The Settings screen.

Everything the user can change about the program is here, in one scrolling
column of cards, in the order a person actually needs them: the Amazon account
first (nothing works without it), then what the program is allowed to buy,
then when it looks, then how it tells you, then the browser it uses, then how
it looks, then the things you need when something has gone wrong, and finally
what this program is.

The rules this screen is built on:

* **Write immediately.** There is no Save button. Every control writes through
  :class:`~app.config.SettingsService` as it changes, because a settings page
  with an unsaved state is a settings page that lies about what the program
  will do.
* **Read the real state, not the stored one, wherever they can differ.** "Start
  with Windows" asks the registry every time, including the switch the user
  may have flipped in Windows Settings; the browser panel asks the browser. A
  screen that reports its own wishes rather than the facts is worse than no
  screen.
* **Two decisions are guarded.** Turning test mode *off* and turning automatic
  purchasing *on* are the only changes here that can cost money, so each needs
  a deliberate confirmation -- and each is reversible with one click
  afterwards.
* **This screen does no work of its own.** Opening Amazon, testing the
  connection, reconnecting, clearing the session, showing the automatic
  purchasing consent and repairing the browser all belong to the window, which
  owns the worker. This page emits a signal and waits.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Sequence

from PySide6.QtCore import Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.branding import BRAND
from app.config import (
    CHECK_INTERVAL_CHOICES,
    CloseButtonAction,
    NotificationKind,
    ThemePreference,
)
from app.core.timeutil import format_duration, format_relative, from_iso
from app.diagnostics.logger import clear_old_logs, log_paths
from app.purchasing.models import ConditionPolicy, PurchaseMode, SellerPolicy
from app.ui.components import (
    ButtonRow,
    Card,
    DangerButton,
    HLine,
    PrimaryButton,
    QuantityField,
    StatusBadge,
    SubtleButton,
    role_label,
)
from app.ui.dialogs import confirm_destructive
from app.ui.theme import StatusSeverity, ThemeManager, ThemeMode
from app.version import BUILD_CHANNEL, VERSION
from app.winint import startup

if TYPE_CHECKING:  # The composition root reaches the automation layer; a
    # screen only needs its shape, so it is not imported at run time.
    from app.ui.app_context import AppContext

logger = logging.getLogger("app.ui.settings")

#: Diagnostics export and screenshot clearing live in a module that is
#: optional at run time: a broken diagnostics helper must not take the
#: settings screen -- and with it the way back to test mode -- down with it.
try:  # pragma: no cover - whichever branch is installed is the one exercised
    from app.diagnostics import report as diagnostic_report
except Exception:  # noqa: BLE001
    diagnostic_report = None  # type: ignore[assignment]
    logger.warning("The diagnostic report tools are unavailable", exc_info=True)

#: Section headings, in display order, with the theme icon each card wears.
#: Used as the keys of :meth:`section`.
AMAZON_ACCOUNT: Final = "Amazon account"
PURCHASING_DEFAULTS: Final = "Purchasing defaults"
MONITORING: Final = "Monitoring"
NOTIFICATIONS: Final = "Notifications"
BROWSER: Final = "Browser"
APPEARANCE: Final = "Appearance"
DIAGNOSTICS: Final = "Diagnostics"
ABOUT: Final = "About"

SECTION_ORDER: Final[tuple[str, ...]] = (
    AMAZON_ACCOUNT,
    PURCHASING_DEFAULTS,
    MONITORING,
    NOTIFICATIONS,
    BROWSER,
    APPEARANCE,
    DIAGNOSTICS,
    ABOUT,
)

_SECTION_ICONS: Final[dict[str, str]] = {
    AMAZON_ACCOUNT: "amazon_account",
    PURCHASING_DEFAULTS: "cart",
    MONITORING: "clock",
    NOTIFICATIONS: "bell",
    BROWSER: "external",
    APPEARANCE: "settings",
    DIAGNOSTICS: "shield",
    ABOUT: "info",
}

#: The label on the test-mode switch. Spelled out rather than abbreviated,
#: because this one line is the difference between a rehearsal and a purchase.
TEST_MODE_LABEL: Final = "Test mode — never place a real order"

_ALWAYS_ON_REASON: Final = "Always on: this tells you money was spent"

_EXPORT_PRIVACY_NOTE: Final = (
    "The report never contains your password, your sign-in cookies or your "
    "card details."
)

#: Where an enum member's own explanation is put on a dropdown item.
_TOOLTIP_ROLE: Final = Qt.ItemDataRole.ToolTipRole

#: Health outcome -> the severity its badge is tinted with. Keyed by the
#: enum's string value so this module does not have to import the health
#: module, which is optional at run time.
_HEALTH_SEVERITIES: Final[dict[str, StatusSeverity]] = {
    "ok": StatusSeverity.SUCCESS,
    "warning": StatusSeverity.WARNING,
    "problem": StatusSeverity.ERROR,
}




class SettingsPage(QWidget):
    """Every user-changeable setting, written through as it is changed."""

    #: Open Amazon in the application's own browser.
    open_amazon_requested = Signal()
    #: Check whether the stored Amazon session still works.
    test_connection_requested = Signal()
    #: Sign in to Amazon again.
    reconnect_requested = Signal()
    #: Sign the application's private browser out of Amazon.
    clear_session_requested = Signal()
    #: Ask the window to show the automatic-purchasing consent dialog.
    auto_buy_consent_requested = Signal()
    #: Re-install the private browser.
    repair_browser_requested = Signal()

    def __init__(
        self,
        context: AppContext,
        theme: ThemeManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._context = context
        self._theme = theme
        self._settings = context.settings
        self._sections: dict[str, Card] = {}

        #: One switch per notification kind, by kind. Public because the
        #: window and the tests both ask this page what it is showing.
        self.notification_switches: dict[NotificationKind, QCheckBox] = {}

        self._build()

    # ---- construction ----------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setObjectName("ScrollAreaFlat")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setAccessibleName("Settings")

        content = QWidget()
        column = QVBoxLayout(content)
        column.setContentsMargins(24, 20, 24, 24)
        column.setSpacing(16)
        self._column = column

        column.addWidget(role_label("Settings", "title", content))

        self._build_account_section()
        self._build_purchasing_section()
        self._build_monitoring_section()
        self._build_notifications_section()
        self._build_browser_section()
        self._build_appearance_section()
        self._build_diagnostics_section()
        self._build_about_section()

        column.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll)

    # ---- small layout helpers -------------------------------------------

    def _add_section(self, title: str, subtitle: str) -> QVBoxLayout:
        """Add a titled card and return the layout its content goes into."""
        card = Card(title, subtitle, _SECTION_ICONS.get(title))
        self._column.addWidget(card)
        self._sections[title] = card
        return card.body_layout

    def _caption(self, text: str, parent: QWidget | None = None) -> QLabel:
        """A muted, wrapping explanation line."""
        label = role_label(text, "caption", parent)
        label.setWordWrap(True)
        return label

    def _labelled(
        self,
        body: QVBoxLayout,
        text: str,
        widget: QWidget,
        help_text: str | None = None,
    ) -> None:
        """Put ``widget`` next to its name, with an optional line underneath."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        label = role_label(text, None)
        label.setMinimumWidth(170)
        label.setWordWrap(True)
        row.addWidget(label)
        row.addWidget(widget, 1)
        body.addLayout(row)
        if help_text:
            body.addWidget(self._caption(help_text))

    def _selectable(self, text: str, parent: QWidget | None = None) -> QLabel:
        """A read-only value the user can select and copy.

        Paths in particular: the only reason to show one is so it can be
        pasted somewhere, and a path that cannot be selected is a path the
        user has to retype.
        """
        label = role_label(text, None, parent)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setWordWrap(True)
        return label

    # ---- 1. Amazon account ----------------------------------------------

    def _build_account_section(self) -> None:
        body = self._add_section(
            AMAZON_ACCOUNT,
            "This program signs in to Amazon in its own private browser "
            "window. The Chrome you use yourself is never touched.",
        )

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(10)
        self.account_badge = StatusBadge("Not connected", StatusSeverity.NEUTRAL)
        status_row.addWidget(self.account_badge)
        self._account_label = role_label("", None)
        self._account_label.setWordWrap(True)
        status_row.addWidget(self._account_label, 1)
        body.addLayout(status_row)

        self._account_checked = self._caption("")
        body.addWidget(self._account_checked)

        open_amazon = SubtleButton("Open Amazon")
        open_amazon.clicked.connect(self.open_amazon_requested)

        test_connection = SubtleButton("Test connection")
        test_connection.clicked.connect(self.test_connection_requested)

        reconnect = PrimaryButton("Reconnect")
        reconnect.setToolTip("Sign in to Amazon again in the app's browser.")
        reconnect.clicked.connect(self.reconnect_requested)

        self.clear_session_button = DangerButton("Clear browser session")
        self.clear_session_button.clicked.connect(self._on_clear_session)

        body.addWidget(
            ButtonRow(
                open_amazon,
                test_connection,
                reconnect,
                self.clear_session_button,
                align="left",
            )
        )
        self._refresh_account()

    def _on_clear_session(self) -> None:
        if not confirm_destructive(
            self,
            title="Clear the saved Amazon sign-in?",
            message=(
                f"This signs {BRAND.display_name}'s own private browser out of "
                "Amazon, so it will have to sign in again before it can check "
                "or buy anything. The Chrome you use yourself, and everything "
                "you are signed in to there, is not touched."
            ),
            confirm_label="Clear sign-in",
        ):
            return
        self.clear_session_requested.emit()

    def _refresh_account(self) -> None:
        current = self._settings.current
        connected = bool(current.amazon_connected)
        self.account_badge.set_status(
            "Connected" if connected else "Not connected",
            StatusSeverity.SUCCESS if connected else StatusSeverity.NEUTRAL,
        )
        if connected:
            self._account_label.setText(
                current.amazon_account_label or "Signed in to Amazon"
            )
        else:
            self._account_label.setText(
                "Not signed in yet. Choose Reconnect to sign in."
            )
        self._account_checked.setText(
            "Last checked "
            f"{format_relative(from_iso(current.amazon_last_verified_at))}"
        )

    # ---- 2. Purchasing defaults -----------------------------------------

    def _build_purchasing_section(self) -> None:
        body = self._add_section(
            PURCHASING_DEFAULTS,
            "The starting rules for every new product you add. You can change "
            "them for one product without changing them here.",
        )
        current = self._settings.current

        self.mode_combo = self._enum_combo(
            self._allowed_modes(),
            current.default_purchase_mode,
            "Default purchase behaviour",
        )
        self._mode_help = self._caption("")
        self._labelled(body, "When the conditions are met", self.mode_combo)
        body.addWidget(self._mode_help)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self.condition_combo = self._enum_combo(
            tuple(ConditionPolicy),
            current.default_condition_policy,
            "Default condition to accept",
        )
        self._condition_help = self._caption("")
        self._labelled(body, "Condition to accept", self.condition_combo)
        body.addWidget(self._condition_help)
        self.condition_combo.currentIndexChanged.connect(self._on_condition_changed)

        self.seller_combo = self._enum_combo(
            tuple(SellerPolicy),
            current.default_seller_policy,
            "Default seller rule",
        )
        self._seller_help = self._caption("")
        self._labelled(body, "Sellers to buy from", self.seller_combo)
        body.addWidget(self._seller_help)
        self.seller_combo.currentIndexChanged.connect(self._on_seller_changed)

        self.quantity_field = QuantityField(current.default_quantity)
        self.quantity_field.setAccessibleName("Default quantity to buy")
        self.quantity_field.value_changed.connect(self._on_quantity_changed)
        self._labelled(
            body,
            "How many to buy",
            self.quantity_field,
            "A larger order is more likely to be stopped by your order total "
            "limit.",
        )

        body.addWidget(HLine())
        body.addWidget(role_label("Test mode", "sectionHeading"))

        self.test_mode_switch = QCheckBox(TEST_MODE_LABEL)
        self.test_mode_switch.setChecked(current.test_mode)
        self.test_mode_switch.setAccessibleName(
            "Test mode. Never place a real order."
        )
        self.test_mode_switch.toggled.connect(self._on_test_mode_toggled)
        body.addWidget(self.test_mode_switch)
        self._test_mode_note = self._caption("")
        body.addWidget(self._test_mode_note)

        # A rule and its own heading, so automatic purchasing is a separate
        # decision on the page and not one more line in a list of defaults.
        body.addWidget(HLine())
        body.addWidget(role_label("Automatic purchasing", "sectionHeading"))
        body.addWidget(
            self._caption(
                "Normally the program gets everything ready and waits for you "
                "to say yes. Automatic purchasing lets it finish the order on "
                "its own, so it can buy something while you are away."
            )
        )

        ack_row = QHBoxLayout()
        ack_row.setContentsMargins(0, 0, 0, 0)
        ack_row.setSpacing(10)
        self.auto_buy_badge = StatusBadge("Not allowed", StatusSeverity.NEUTRAL)
        ack_row.addWidget(self.auto_buy_badge)
        self._auto_buy_label = role_label("", None)
        self._auto_buy_label.setWordWrap(True)
        ack_row.addWidget(self._auto_buy_label, 1)
        body.addLayout(ack_row)

        self.auto_buy_enable_button = DangerButton("Turn on automatic purchasing")
        self.auto_buy_enable_button.clicked.connect(self.auto_buy_consent_requested)
        self.auto_buy_disable_button = SubtleButton("Turn off automatic purchasing")
        self.auto_buy_disable_button.clicked.connect(
            lambda: self.set_auto_buy_acknowledged(False)
        )
        body.addWidget(
            ButtonRow(
                self.auto_buy_enable_button,
                self.auto_buy_disable_button,
                align="left",
            )
        )

        self._refresh_test_mode_note()
        self._refresh_auto_buy()
        self._refresh_choice_help()

    def _allowed_modes(self) -> tuple[PurchaseMode, ...]:
        """The purchase modes the user may pick right now.

        Automatic is absent from the list until it has been consented to, so
        it cannot be selected by scrolling a dropdown past it.
        """
        if self._settings.current.auto_buy_acknowledged:
            return tuple(PurchaseMode)
        return (PurchaseMode.ASSISTED,)

    def _enum_combo(
        self, options: Sequence[Any], current: Any, accessible_name: str
    ) -> QComboBox:
        """A dropdown of enum members, labelled and described by the enum."""
        combo = QComboBox()
        combo.setAccessibleName(accessible_name)
        self._fill_enum_combo(combo, options, current)
        return combo

    def _fill_enum_combo(
        self, combo: QComboBox, options: Sequence[Any], current: Any
    ) -> None:
        """Repopulate ``combo`` without letting the refill look like a choice."""
        combo.blockSignals(True)
        combo.clear()
        for option in options:
            combo.addItem(option.label, option.value)
            description = getattr(option, "description", None)
            if description:
                combo.setItemData(combo.count() - 1, description, _TOOLTIP_ROLE)
        index = combo.findData(getattr(current, "value", current))
        combo.setCurrentIndex(max(index, 0))
        combo.blockSignals(False)

    def _selected(self, combo: QComboBox, enum_type: Any) -> Any | None:
        """The enum member behind the current item, or ``None``."""
        try:
            return enum_type(combo.currentData())
        except (TypeError, ValueError):
            return None

    def _on_mode_changed(self) -> None:
        chosen = self._selected(self.mode_combo, PurchaseMode)
        if chosen is None:
            return
        self._settings.update(default_purchase_mode=chosen)
        self._refresh_choice_help()

    def _on_condition_changed(self) -> None:
        chosen = self._selected(self.condition_combo, ConditionPolicy)
        if chosen is None:
            return
        self._settings.update(default_condition_policy=chosen)
        self._refresh_choice_help()

    def _on_seller_changed(self) -> None:
        chosen = self._selected(self.seller_combo, SellerPolicy)
        if chosen is None:
            return
        self._settings.update(default_seller_policy=chosen)
        self._refresh_choice_help()

    def _on_quantity_changed(self, value: int) -> None:
        self._settings.update(default_quantity=int(value))

    def _refresh_choice_help(self) -> None:
        """Show the explanation for whatever is selected in each dropdown.

        Not every one of these enumerations carries a ``description``, so each
        one also has a sentence of its own to fall back on rather than leaving
        a dropdown unexplained.
        """
        for combo, label, enum_type, fallback in (
            (
                self.mode_combo,
                self._mode_help,
                PurchaseMode,
                "The program gets the order ready and waits for you to say yes.",
            ),
            (
                self.condition_combo,
                self._condition_help,
                ConditionPolicy,
                "Only an offer in a condition you allow is ever bought.",
            ),
            (self.seller_combo, self._seller_help, SellerPolicy, ""),
        ):
            chosen = self._selected(combo, enum_type)
            description = getattr(chosen, "description", None)
            if chosen is PurchaseMode.AUTOMATIC:
                description = (
                    "The program places the order itself, without asking you "
                    "first."
                )
            label.setText(description or fallback)
            label.setVisible(bool(label.text()))

    def _on_test_mode_toggled(self, enabled: bool) -> None:
        """Guard the one switch that decides whether money can be spent."""
        if enabled:
            self._settings.update(test_mode=True)
            self._refresh_test_mode_note()
            return

        if not confirm_destructive(
            self,
            title="Allow real orders to be placed?",
            message=(
                "With test mode off, this program can place real orders on "
                "Amazon and real money can be spent. Every rule you have set "
                "is still checked first, but an order that passes those "
                "checks will be placed without stopping to ask you."
            ),
            confirm_label="Allow real orders",
        ):
            # Leaving it on is the safe answer, and the switch has to show it.
            self.test_mode_switch.blockSignals(True)
            self.test_mode_switch.setChecked(True)
            self.test_mode_switch.blockSignals(False)
            self._refresh_test_mode_note()
            return

        self._settings.update(test_mode=False)
        logger.info("Test mode turned off by the user")
        self._refresh_test_mode_note()

    def _refresh_test_mode_note(self) -> None:
        if self._settings.current.test_mode:
            self._test_mode_note.setText(
                "Test mode is on. The program goes right up to the last step "
                "and then stops, so nothing is ever ordered."
            )
        else:
            self._test_mode_note.setText(
                "Test mode is off. Real orders can be placed and real money "
                "can be spent."
            )

    def set_auto_buy_acknowledged(self, acknowledged: bool) -> None:
        """Record the result of the automatic-purchasing consent dialog.

        Turning it off also puts the default back to waiting for the user, so
        a product added afterwards cannot inherit a permission that has been
        withdrawn.
        """
        changes: dict[str, Any] = {"auto_buy_acknowledged": bool(acknowledged)}
        if not acknowledged:
            changes["default_purchase_mode"] = PurchaseMode.ASSISTED
        self._settings.update(**changes)
        logger.info(
            "Automatic purchasing %s", "allowed" if acknowledged else "turned off"
        )
        self._fill_enum_combo(
            self.mode_combo,
            self._allowed_modes(),
            self._settings.current.default_purchase_mode,
        )
        self._refresh_auto_buy()
        self._refresh_choice_help()

    def _refresh_auto_buy(self) -> None:
        acknowledged = bool(self._settings.current.auto_buy_acknowledged)
        self.auto_buy_badge.set_status(
            "Allowed" if acknowledged else "Not allowed",
            StatusSeverity.WARNING if acknowledged else StatusSeverity.NEUTRAL,
        )
        if acknowledged:
            self._auto_buy_label.setText(
                "You have allowed automatic purchasing. It can now be chosen "
                "above."
            )
        else:
            self._auto_buy_label.setText(
                "The program will always ask you before it buys anything."
            )
        self.auto_buy_enable_button.setVisible(not acknowledged)
        self.auto_buy_disable_button.setVisible(acknowledged)

    # ---- 3. Monitoring ---------------------------------------------------

    def _build_monitoring_section(self) -> None:
        body = self._add_section(
            MONITORING,
            "How often the program looks at a product's page, and what "
            "happens when you close the window.",
        )
        current = self._settings.current

        self.interval_combo = QComboBox()
        self.interval_combo.setAccessibleName("How often to check a product")
        for seconds in CHECK_INTERVAL_CHOICES:
            self.interval_combo.addItem(f"Every {format_duration(seconds)}", seconds)
        index = self.interval_combo.findData(current.default_check_interval_seconds)
        self.interval_combo.setCurrentIndex(max(index, 0))
        self.interval_combo.currentIndexChanged.connect(self._on_interval_changed)
        self._labelled(
            body,
            "Check a product",
            self.interval_combo,
            "Checking more often will not make a product come back in stock "
            "any sooner. The times are spread out slightly so the pattern is "
            "not perfectly regular.",
        )

        self.monitoring_switch = QCheckBox("Keep watching my products")
        self.monitoring_switch.setChecked(current.monitoring_enabled)
        self.monitoring_switch.setAccessibleName("Keep watching my products")
        self.monitoring_switch.toggled.connect(
            lambda enabled: self._settings.update(monitoring_enabled=bool(enabled))
        )
        body.addWidget(self.monitoring_switch)

        self.tray_switch = QCheckBox("Continue running in the system tray")
        self.tray_switch.setChecked(current.continue_in_tray)
        self.tray_switch.setAccessibleName("Continue running in the system tray")
        self.tray_switch.toggled.connect(
            lambda enabled: self._settings.update(continue_in_tray=bool(enabled))
        )
        body.addWidget(self.tray_switch)
        body.addWidget(
            self._caption(
                "While it runs in the tray the program keeps checking your "
                "products. Close it from the tray icon to stop completely."
            )
        )

        body.addWidget(HLine())
        body.addWidget(
            role_label("When I press the window's close button", "sectionHeading")
        )
        self._close_group = QButtonGroup(self)
        for action in CloseButtonAction:
            radio = QRadioButton(action.label)
            radio.setAccessibleName(action.label)
            radio.setChecked(action is current.close_button_action)
            radio.toggled.connect(
                lambda checked, chosen=action: checked
                and self._settings.update(close_button_action=chosen)
            )
            self._close_group.addButton(radio)
            body.addWidget(radio)

        body.addWidget(HLine())
        self.startup_switch = QCheckBox("Start with Windows")
        self.startup_switch.setAccessibleName("Start with Windows")
        body.addWidget(self.startup_switch)
        self._startup_note = self._caption("")
        body.addWidget(self._startup_note)

        self.startup_settings_button = SubtleButton("Open Windows startup settings")
        self.startup_settings_button.clicked.connect(self._on_open_startup_settings)
        body.addWidget(ButtonRow(self.startup_settings_button, align="left"))

        # Connected after the first read so filling the switch in does not
        # write to the registry.
        self._refresh_startup()
        self.startup_switch.toggled.connect(self._on_startup_toggled)

    def _on_interval_changed(self) -> None:
        seconds = self.interval_combo.currentData()
        if seconds is None:
            return
        self._settings.update(default_check_interval_seconds=int(seconds))

    def _on_startup_toggled(self, wanted: bool) -> None:
        """Write the registry entry, then show whatever Windows now reports."""
        try:
            if wanted:
                startup.enable()
            else:
                startup.disable()
        except Exception:  # noqa: BLE001 - registry access can fail
            logger.warning("Could not change start with Windows", exc_info=True)
        self._refresh_startup()

    def _on_open_startup_settings(self) -> None:
        try:
            startup.open_windows_startup_settings()
        except Exception:  # noqa: BLE001
            logger.warning(
                "Could not open the Windows startup settings", exc_info=True
            )

    def _refresh_startup(self) -> None:
        """Re-read the real state and make the screen agree with it."""
        status = None
        try:
            status = startup.status()
        except Exception:  # noqa: BLE001
            logger.warning(
                "Could not read the start with Windows state", exc_info=True
            )

        if status is None:
            self.startup_switch.setEnabled(False)
            self._startup_note.setText(
                "This computer cannot be asked whether the program starts "
                "with Windows."
            )
            self.startup_settings_button.setVisible(False)
            return

        self.startup_switch.blockSignals(True)
        self.startup_switch.setChecked(bool(status.effective))
        self.startup_switch.blockSignals(False)

        if status.disabled_by_windows:
            self._startup_note.setText(
                "Turned off in Windows Settings. Until you turn it back on "
                "there, this program will not start by itself."
            )
        elif status.effective:
            self._startup_note.setText(
                "The program starts quietly in the tray when you sign in to "
                "Windows."
            )
        else:
            self._startup_note.setText(
                "The program only runs when you open it yourself."
            )
        self.startup_settings_button.setVisible(bool(status.disabled_by_windows))

        # Keep the stored value in step with the registry, but only when it
        # has actually drifted: this runs on every refresh, and a settings
        # write publishes a new snapshot to every listener.
        if self._settings.current.start_with_windows != bool(status.effective):
            self._settings.update(start_with_windows=bool(status.effective))

    # ---- 4. Notifications ------------------------------------------------

    def _build_notifications_section(self) -> None:
        body = self._add_section(
            NOTIFICATIONS,
            "What the program tells you about while it is running. Anything "
            "that means money was spent, or nearly was, is always shown.",
        )
        current = self._settings.current

        self.notifications_master = QCheckBox("Show notifications")
        self.notifications_master.setChecked(current.notifications_enabled)
        self.notifications_master.setAccessibleName("Show notifications")
        self.notifications_master.toggled.connect(self._on_notifications_master)
        body.addWidget(self.notifications_master)

        self._channel_label = self._caption("")
        body.addWidget(self._channel_label)
        body.addWidget(HLine())

        for kind in NotificationKind:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(10)
            box = QCheckBox(kind.label)
            box.setAccessibleName(kind.label)
            if kind.always_on:
                box.setChecked(True)
                box.setEnabled(False)
                box.setToolTip(_ALWAYS_ON_REASON)
                box.setAccessibleDescription(_ALWAYS_ON_REASON)
                row.addWidget(box)
                row.addWidget(self._caption(_ALWAYS_ON_REASON), 1)
            else:
                box.setChecked(
                    bool(current.notification_toggles.get(kind.value, True))
                )
                box.toggled.connect(
                    lambda enabled, chosen=kind: self._on_notification_toggled(
                        chosen, bool(enabled)
                    )
                )
                row.addWidget(box, 1)
            self.notification_switches[kind] = box
            body.addLayout(row)

        body.addWidget(HLine())
        self.test_notification_button = SubtleButton("Send a test notification")
        body.addWidget(ButtonRow(self.test_notification_button, align="left"))
        self._notification_result = self._caption("")
        self._notification_result.setVisible(False)
        body.addWidget(self._notification_result)
        self.test_notification_button.clicked.connect(self._on_test_notification)

        self._refresh_notifications()

    def _on_notifications_master(self, enabled: bool) -> None:
        self._settings.update(notifications_enabled=bool(enabled))
        self._refresh_notifications()

    def _on_notification_toggled(self, kind: NotificationKind, enabled: bool) -> None:
        try:
            self._settings.set_notification(kind, enabled)
        except ValueError:
            # An always-on kind. Its box is disabled, so this cannot be
            # reached through the interface, but the switch must not be left
            # showing something the program will not do.
            box = self.notification_switches.get(kind)
            if box is not None:
                box.blockSignals(True)
                box.setChecked(True)
                box.blockSignals(False)

    def _refresh_notifications(self) -> None:
        enabled = self._settings.current.notifications_enabled
        for kind, box in self.notification_switches.items():
            if not kind.always_on:
                box.setEnabled(enabled)

        channel = "Not known"
        notifier = getattr(self._context, "notifier", None)
        reader = getattr(notifier, "available_channel", None)
        if callable(reader):
            try:
                channel = str(reader())
            except Exception:  # noqa: BLE001
                logger.debug("Could not read the notification channel", exc_info=True)
        self._channel_label.setText(f"Notifications reach you by: {channel}")

    def _on_test_notification(self) -> None:
        notifier = getattr(self._context, "notifier", None)
        tester = getattr(notifier, "self_test", None)
        if not callable(tester):
            message = (
                "Notifications are not available on this computer, so there "
                "is nothing to test."
            )
        else:
            try:
                _delivered, message = tester()
            except Exception:  # noqa: BLE001
                logger.warning("The notification test failed", exc_info=True)
                message = (
                    "The test could not be sent. The checks further down this "
                    "page may say why."
                )
        self._notification_result.setText(str(message))
        self._notification_result.setVisible(True)

    # ---- 5. Browser ------------------------------------------------------

    #: The read-only browser details, in the order they are shown.
    _BROWSER_FIELDS: Final[tuple[str, ...]] = (
        "Browser",
        "Program file",
        "Sign-in folder",
        "Playwright version",
        "Running now",
    )

    def _build_browser_section(self) -> None:
        body = self._add_section(
            BROWSER,
            "The program uses its own private copy of a browser, kept in its "
            "own folder, so nothing it does can disturb the browser you use.",
        )

        self._browser_rows: dict[str, QLabel] = {}
        for name in self._BROWSER_FIELDS:
            value = self._selectable("")
            self._browser_rows[name] = value
            self._labelled(body, name, value)

        open_profile = SubtleButton("Open profile folder")
        open_profile.clicked.connect(self._on_open_profile_folder)
        repair = SubtleButton("Repair browser")
        repair.setToolTip("Download and install the private browser again.")
        repair.clicked.connect(self.repair_browser_requested)
        body.addWidget(ButtonRow(open_profile, repair, align="left"))

        self._refresh_browser()

    def _browser_info(self) -> Any | None:
        browser = getattr(self._context, "browser", None)
        reader = getattr(browser, "info", None)
        if not callable(reader):
            return None
        try:
            return reader()
        except Exception:  # noqa: BLE001
            logger.warning("Could not read the browser details", exc_info=True)
            return None

    def _refresh_browser(self) -> None:
        info = self._browser_info()
        if info is None:
            for label in self._browser_rows.values():
                label.setText("Not available")
            return
        self._browser_rows["Browser"].setText(
            getattr(info, "summary", None) or "Not installed yet"
        )
        self._browser_rows["Program file"].setText(
            info.executable_path or "Not installed yet"
        )
        self._browser_rows["Sign-in folder"].setText(info.profile_dir)
        self._browser_rows["Playwright version"].setText(
            info.playwright_version or "Not available"
        )
        self._browser_rows["Running now"].setText("Yes" if info.running else "No")

    def _on_open_profile_folder(self) -> None:
        info = self._browser_info()
        folder = getattr(info, "profile_dir", None) if info is not None else None
        self._open_folder(
            Path(folder) if folder else self._context.paths.browser_profile_dir
        )

    def _open_folder(self, folder: Path) -> None:
        """Show ``folder`` in Explorer, creating it if it is not there yet."""
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("Could not create the folder %s", folder)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ---- 6. Appearance ---------------------------------------------------

    def _build_appearance_section(self) -> None:
        body = self._add_section(
            APPEARANCE,
            "Match Windows follows the light or dark setting you chose for "
            "Windows itself.",
        )
        stored = self._settings.current.theme
        self._theme_group = QButtonGroup(self)
        for mode in ThemeMode:
            radio = QRadioButton(mode.label)
            radio.setAccessibleName(f"{mode.label} appearance")
            radio.setChecked(mode.value == stored.value)
            radio.toggled.connect(
                lambda checked, chosen=mode: checked and self._on_theme_chosen(chosen)
            )
            self._theme_group.addButton(radio)
            body.addWidget(radio)

    def _on_theme_chosen(self, mode: ThemeMode) -> None:
        self._theme.set_mode(mode)
        self._settings.update(theme=ThemePreference(mode.value))

    # ---- 7. Diagnostics --------------------------------------------------

    def _build_diagnostics_section(self) -> None:
        body = self._add_section(
            DIAGNOSTICS,
            "If something is not working, these are the things to try and the "
            "file to send on.",
        )

        open_logs = SubtleButton("Open logs folder")
        open_logs.clicked.connect(
            lambda: self._open_folder(self._context.paths.logs_dir)
        )
        self.export_button = PrimaryButton("Export diagnostic report")
        self.export_button.clicked.connect(self._on_export_report)
        body.addWidget(ButtonRow(open_logs, self.export_button, align="left"))
        body.addWidget(self._caption(_EXPORT_PRIVACY_NOTE))

        clear_logs = SubtleButton("Clear old logs")
        delete_shots = DangerButton("Delete saved screenshots")
        clear_logs.clicked.connect(self._on_clear_old_logs)
        delete_shots.clicked.connect(self._on_delete_screenshots)
        body.addWidget(ButtonRow(clear_logs, delete_shots, align="left"))

        text_log, _event_log = log_paths(self._context.paths.logs_dir)
        body.addWidget(self._selectable(str(text_log.parent)))

        self._diagnostics_result = self._caption("")
        self._diagnostics_result.setVisible(False)
        body.addWidget(self._diagnostics_result)

        body.addWidget(HLine())
        body.addWidget(role_label("Health", "sectionHeading"))
        self._health_summary = role_label("", None)
        self._health_summary.setWordWrap(True)
        body.addWidget(self._health_summary)

        self._health_body = QVBoxLayout()
        self._health_body.setContentsMargins(0, 0, 0, 0)
        self._health_body.setSpacing(10)
        body.addLayout(self._health_body)

        self.health_rerun_button = SubtleButton("Re-run checks")
        self.health_rerun_button.clicked.connect(self.run_health_checks)
        body.addWidget(ButtonRow(self.health_rerun_button, align="left"))

        self.run_health_checks()

    def _report_message(self, text: str) -> None:
        self._diagnostics_result.setText(text)
        self._diagnostics_result.setVisible(True)

    def _on_export_report(self) -> None:
        if diagnostic_report is None or not hasattr(diagnostic_report, "export_zip"):
            self._report_message(
                "The report tools are not available in this copy of the "
                "program."
            )
            return

        suggested = str(Path.home() / f"{BRAND.data_folder_name}-diagnostics.zip")
        chosen, _selected_filter = QFileDialog.getSaveFileName(
            self, "Save the diagnostic report", suggested, "Zip archive (*.zip)"
        )
        if not chosen:
            return
        try:
            written = diagnostic_report.export_zip(
                Path(chosen),
                paths=self._context.paths,
                database=self._context.database,
                settings=self._settings,
                activity=self._context.repositories.activity,
                browser_manager=getattr(self._context, "browser", None),
                notifier=getattr(self._context, "notifier", None),
            )
        except Exception:  # noqa: BLE001
            logger.warning("Could not write the diagnostic report", exc_info=True)
            self._report_message(
                "The report could not be saved there. Try a folder you know "
                "you can write to, such as your Desktop."
            )
            return
        self._report_message(f"Saved to {written}")

    def _on_clear_old_logs(self) -> None:
        try:
            removed = clear_old_logs(self._context.paths.logs_dir)
        except Exception:  # noqa: BLE001
            logger.warning("Could not clear the old logs", exc_info=True)
            self._report_message("The old log files could not be removed.")
            return
        if removed == 0:
            self._report_message("There were no old log files to remove.")
        elif removed == 1:
            self._report_message("One old log file was removed.")
        else:
            self._report_message(f"{removed} old log files were removed.")

    def _on_delete_screenshots(self) -> None:
        if diagnostic_report is None or not hasattr(
            diagnostic_report, "clear_diagnostics"
        ):
            self._report_message(
                "The screenshot tools are not available in this copy of the "
                "program."
            )
            return
        if not confirm_destructive(
            self,
            title="Delete the saved screenshots?",
            message=(
                "The program saves a picture of the page when an automatic "
                "step fails unexpectedly, so the problem can be explained "
                "later. Deleting them cannot be undone, and it does not "
                "affect anything else."
            ),
            confirm_label="Delete screenshots",
        ):
            return
        try:
            removed = diagnostic_report.clear_diagnostics(
                self._context.paths, self._context.repositories.activity
            )
        except Exception:  # noqa: BLE001
            logger.warning("Could not delete the screenshots", exc_info=True)
            self._report_message("The screenshots could not be deleted.")
            return
        if removed == 0:
            self._report_message("There were no saved screenshots.")
        elif removed == 1:
            self._report_message("One saved screenshot was deleted.")
        else:
            self._report_message(f"{removed} saved screenshots were deleted.")

    @Slot()
    def run_health_checks(self) -> None:
        """Run the checks and redraw the health rows."""
        while self._health_body.count():
            item = self._health_body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        health = getattr(self._context, "health", None)
        runner = getattr(health, "run", None)
        if not callable(runner):
            self._health_summary.setText(
                "The checks are not available in this copy of the program."
            )
            return
        try:
            report = runner()
        except Exception:  # noqa: BLE001
            logger.warning("The health checks could not be run", exc_info=True)
            self._health_summary.setText("The checks could not be run.")
            return

        self._health_summary.setText(str(getattr(report, "summary", "")))
        for check in getattr(report, "checks", ()):
            self._health_body.addWidget(self._build_health_row(check))

    def _build_health_row(self, check: Any) -> QWidget:
        row = QWidget()
        layout = QVBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        outcome = getattr(check, "outcome", None)
        word = str(getattr(outcome, "label", "") or "")
        name = str(getattr(check, "name", ""))

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        head.addWidget(role_label(name, None, row))
        badge = StatusBadge(
            word,
            _HEALTH_SEVERITIES.get(str(outcome), StatusSeverity.NEUTRAL),
            row,
        )
        badge.setAccessibleName(f"{name}: {word}")
        head.addWidget(badge)
        head.addStretch(1)
        layout.addLayout(head)

        detail = str(getattr(check, "detail", "") or "")
        if detail:
            layout.addWidget(self._caption(detail, row))
        hint = getattr(check, "fix_hint", None)
        if hint:
            layout.addWidget(self._caption(f"What to do: {hint}", row))
        return row

    # ---- 8. About --------------------------------------------------------

    def _build_about_section(self) -> None:
        body = self._add_section(ABOUT, BRAND.tagline)

        body.addWidget(role_label(BRAND.display_name, "sectionHeading"))
        channel = "Release" if BUILD_CHANNEL == "release" else "Preview build"
        body.addWidget(self._selectable(f"Version {VERSION} — {channel}"))

        disclaimer = role_label(BRAND.disclaimer, None)
        disclaimer.setWordWrap(True)
        body.addWidget(disclaimer)

    # ---- public API ------------------------------------------------------

    def refresh(self) -> None:
        """Re-read everything this screen shows from its real source."""
        self._refresh_account()
        self._refresh_test_mode_note()
        self._refresh_auto_buy()
        self._refresh_choice_help()
        self._refresh_startup()
        self._refresh_notifications()
        self._refresh_browser()

    def section_titles(self) -> tuple[str, ...]:
        """The headings of the cards on this screen, in display order."""
        return tuple(title for title in SECTION_ORDER if title in self._sections)

    def section(self, title: str) -> Card | None:
        """The card for ``title``, or ``None`` when it was not built."""
        return self._sections.get(title)
