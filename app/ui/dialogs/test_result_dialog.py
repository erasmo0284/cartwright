"""The result of a test-mode run.

A test run does everything a real purchase does except click the final
button: it opens the product, validates the rules, prepares an isolated
order, goes through checkout, re-checks the total and locates Amazon's order
control. That makes it the honest way to find out whether a purchase would
work -- including whether Amazon's layout still matches what the app expects.

The dialog therefore states two things unmistakably: what passed, and that no
order was submitted.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.branding import BRAND
from app.purchasing.purchase_service import TestRunResult
from app.ui.components import (
    ButtonRow,
    Card,
    GuardTable,
    PrimaryButton,
    StatusBadge,
    SubtleButton,
)
from app.ui.theme import StatusSeverity, font_body, font_title

logger = logging.getLogger("app.ui.dialogs.test_result")


class TestResultDialog(QDialog):
    """Reports a completed test run."""

    def __init__(self, result: TestRunResult, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._result = result

        self.setWindowTitle(f"Test result - {BRAND.display_name}")
        self.setModal(True)
        self.setMinimumSize(540, 600)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(20, 18, 20, 8)
        header_layout.setSpacing(6)

        heading = QLabel(result.headline)
        heading.setFont(font_title())
        heading.setProperty("role", "title")
        header_layout.addWidget(heading)

        badge = StatusBadge()
        if result.succeeded:
            badge.set_status("No order was submitted", StatusSeverity.SUCCESS)
        else:
            badge.set_status("No order was submitted", StatusSeverity.WARNING)
        header_layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignLeft)

        summary = QLabel(self._summary_text())
        summary.setFont(font_body())
        summary.setWordWrap(True)
        header_layout.addWidget(summary)
        outer.addWidget(header)

        scroll = QScrollArea()
        scroll.setObjectName("ScrollAreaFlat")
        scroll.setWidgetResizable(True)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(20, 8, 20, 16)
        body_layout.setSpacing(12)

        body_layout.addWidget(self._build_checks_card())
        body_layout.addWidget(self._build_totals_card())
        body_layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        footer = QWidget()
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(20, 12, 20, 16)
        notice = QLabel(
            "Test mode is on, so nothing can be ordered. Turn test mode off in "
            "Settings when you are ready to place real orders."
        )
        notice.setProperty("role", "caption")
        notice.setWordWrap(True)
        footer_layout.addWidget(notice)

        close = PrimaryButton("Done")
        close.clicked.connect(self.accept)
        close.setDefault(True)
        settings = SubtleButton("Open Settings")
        settings.clicked.connect(self._on_open_settings)
        footer_layout.addWidget(ButtonRow(settings, close, align="right"))
        outer.addWidget(footer)

        self._open_settings_requested = False

        logger.info(
            "Showing a test result",
            extra={
                "purchase_job_id": result.purchase_job_id,
                "passed": result.report.passed,
                "order_button_located": result.order_button_located,
            },
        )

    def _summary_text(self) -> str:
        if self._result.succeeded:
            return (
                f"Everything needed to buy {self._result.product.display_title} "
                "checked out, and Amazon's order button was found. The button "
                "was NOT clicked and no order was submitted."
            )
        if not self._result.report.passed:
            failures = self._result.report.blocking_failures
            first = failures[0].title if failures else "a required check"
            return (
                f"The run stopped because {first.lower()} did not pass. Nothing "
                "was ordered. Fix that and run the test again."
            )
        return (
            "The checks passed, but Amazon's order button could not be found. "
            "That usually means Amazon changed its checkout page. Nothing was "
            "ordered."
        )

    def _build_checks_card(self) -> Card:
        card = Card(title="Checks")
        table = GuardTable()
        table.set_report(self._result.report)
        card.add_widget(table)

        row = QGridLayout()
        row.setColumnStretch(0, 1)
        label = QLabel("Final order button")
        label.setProperty("role", "caption")
        row.addWidget(label, 0, 0)
        found = QLabel("Located" if self._result.order_button_located else "Not found")
        found.setFont(font_body())
        found.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        row.addWidget(found, 0, 1)
        card.add_layout(row)
        return card

    def _build_totals_card(self) -> Card:
        checkout = self._result.checkout
        card = Card(title="What the order would have cost")
        grid = QGridLayout()
        grid.setColumnStretch(0, 1)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)

        rows: list[tuple[str, str]] = []
        for label, amount in (
            ("Item", checkout.item_subtotal),
            ("Shipping", checkout.shipping),
            ("Tax", checkout.tax),
            ("Order total", checkout.order_total),
        ):
            if amount is not None:
                rows.append((label, amount.format()))
        rows.append(("Delivering to", checkout.address_label or "Not shown"))
        rows.append(("Paying with", checkout.payment_label or "Not shown"))

        for index, (label, value) in enumerate(rows):
            key = QLabel(label)
            key.setProperty("role", "caption")
            grid.addWidget(key, index, 0)
            value_label = QLabel(value)
            value_label.setFont(font_body())
            value_label.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            value_label.setWordWrap(True)
            grid.addWidget(value_label, index, 1)

        card.add_layout(grid)
        return card

    def _on_open_settings(self) -> None:
        self._open_settings_requested = True
        self.accept()

    @property
    def open_settings_requested(self) -> bool:
        return self._open_settings_requested
