"""Reporting a blocked purchase.

The wording here matters because a blocked purchase is the guard working, not
a malfunction. The dialog therefore leads with the specific rule that stopped
it, shows expected against found, and states in the first sentence that no
order was placed -- which is the thing a worried user most needs to read.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QScrollArea, QVBoxLayout, QWidget

from app.branding import BRAND
from app.core.errors import ActionHint
from app.purchasing.purchase_service import BlockedOutcome
from app.ui.components import (
    ButtonRow,
    Card,
    GuardTable,
    PrimaryButton,
    StatusBadge,
    SubtleButton,
)
from app.ui.dialogs.error_dialog import ACTION_LABELS
from app.ui.theme import StatusSeverity, font_body, font_title

logger = logging.getLogger("app.ui.dialogs.blocked")


class BlockedDialog(QDialog):
    """Explains why a purchase was refused."""

    def __init__(self, outcome: BlockedOutcome, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._outcome = outcome
        self._chosen: ActionHint | None = None

        self.setWindowTitle(f"Purchase blocked - {BRAND.display_name}")
        self.setModal(True)
        self.setMinimumSize(520, 520)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(20, 18, 20, 8)
        header_layout.setSpacing(8)

        heading = QLabel("Purchase blocked")
        heading.setFont(font_title())
        heading.setProperty("role", "title")
        header_layout.addWidget(heading)

        badge = StatusBadge()
        badge.set_status("No order was placed", StatusSeverity.BLOCKED)
        header_layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignLeft)

        reason = QLabel(self._reason_text())
        reason.setFont(font_body())
        reason.setWordWrap(True)
        reason.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        header_layout.addWidget(reason)
        outer.addWidget(header)

        scroll = QScrollArea()
        scroll.setObjectName("ScrollAreaFlat")
        scroll.setWidgetResizable(True)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(20, 8, 20, 16)
        body_layout.setSpacing(12)

        comparison = self._build_comparison_card()
        if comparison is not None:
            body_layout.addWidget(comparison)

        if outcome.report is not None:
            card = Card(title="What was checked")
            table = GuardTable()
            table.set_report(outcome.report)
            card.add_widget(table)
            body_layout.addWidget(card)

        body_layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        outer.addWidget(self._build_footer())

        logger.info(
            "Showing a blocked purchase",
            extra={
                "purchase_job_id": outcome.purchase_job_id,
                "code": outcome.error.code.value,
            },
        )

    def _reason_text(self) -> str:
        """Lead with the specific rule, then reassure about the order."""
        report = self._outcome.report
        if report is not None and report.blocking_failures:
            check = report.blocking_failures[0]
            detail = check.detail or ""
            return (
                f"{check.title} did not pass, so the order was not placed. "
                f"{detail}".strip()
            )
        return self._outcome.error.detail

    def _build_comparison_card(self) -> Card | None:
        report = self._outcome.report
        if report is None or not report.blocking_failures:
            return None
        check = report.blocking_failures[0]
        if not check.expected and not check.actual:
            return None

        card = Card(title=check.title)
        for label, value in (("Expected", check.expected), ("Found", check.actual)):
            if not value:
                continue
            key = QLabel(label)
            key.setProperty("role", "caption")
            card.add_widget(key)
            shown = QLabel(str(value))
            shown.setFont(font_body())
            shown.setWordWrap(True)
            shown.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            card.add_widget(shown)
        return card

    def _build_footer(self) -> QWidget:
        footer = QWidget()
        layout = QVBoxLayout(footer)
        layout.setContentsMargins(20, 12, 20, 16)
        layout.setSpacing(8)

        buttons: list[QWidget] = []
        for hint in self._outcome.error.presentation.actions:
            label = ACTION_LABELS.get(hint)
            if not label:
                continue
            button = SubtleButton(label)
            button.setAutoDefault(False)
            button.clicked.connect(
                lambda _checked=False, chosen=hint: self._choose(chosen)
            )
            buttons.append(button)

        close = PrimaryButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.reject)
        buttons.append(close)

        layout.addWidget(ButtonRow(*buttons, align="right"))
        return footer

    def _choose(self, hint: ActionHint) -> None:
        self._chosen = hint
        self.accept()

    @property
    def chosen_action(self) -> ActionHint | None:
        return self._chosen
