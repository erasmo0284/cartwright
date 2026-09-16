"""Presenting an :class:`~app.core.errors.AppError` to a person.

Every failure the application can hit already carries a plain-English title,
an explanation and a set of suggested next steps (see
:mod:`app.core.errors`). This module renders that, and nothing else: there is
no path here for a raw exception message or an error code to reach the
screen, which is what keeps "Something went wrong" out of the product.

The action buttons come from the error's own :class:`ActionHint` list. The
dialog does not know how to perform them -- it reports which one the user
chose, and the caller does the work.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.branding import BRAND
from app.core.errors import ActionHint, AppError, Severity
from app.ui.theme import IconSet, StatusSeverity, font_body, font_title

logger = logging.getLogger("app.ui.dialogs.error")

#: Button text for each suggested next step. Verbs, so the user can tell what
#: will happen before clicking.
ACTION_LABELS: dict[ActionHint, str] = {
    ActionHint.RETRY: "Try again",
    ActionHint.CONNECT_AMAZON: "Connect Amazon",
    ActionHint.OPEN_BROWSER: "Open Amazon",
    ActionHint.SHOW_BROWSER_WINDOW: "Show the browser",
    ActionHint.RECONNECT: "Sign in again",
    ActionHint.CLEAR_SESSION: "Clear saved session",
    ActionHint.EDIT_RULES: "Change my rules",
    ActionHint.CHECK_PRODUCT_AGAIN: "Check the product again",
    ActionHint.OPEN_PRODUCT_PAGE: "Open the product page",
    ActionHint.OPEN_CART: "Open my cart",
    ActionHint.OPEN_AMAZON_ORDERS: "Open my Amazon orders",
    ActionHint.REVIEW_AND_CONFIRM: "Review it again",
    ActionHint.PAUSE_MONITORING: "Pause monitoring",
    ActionHint.OPEN_LOGS: "Open logs folder",
    ActionHint.RUN_DIAGNOSTICS: "Run diagnostics",
    ActionHint.REPAIR_BROWSER: "Repair the browser",
}

#: Icon and badge colour per severity.
_SEVERITY_LOOK: dict[Severity, tuple[str, StatusSeverity]] = {
    Severity.INFO: ("info", StatusSeverity.INFO),
    Severity.WARNING: ("warning", StatusSeverity.WARNING),
    Severity.BLOCKED: ("shield", StatusSeverity.BLOCKED),
    Severity.ERROR: ("cross", StatusSeverity.ERROR),
}


class ErrorDialog(QDialog):
    """Shows one failure, with its suggested next steps as buttons."""

    def __init__(self, error: AppError, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._error = error
        self._chosen: ActionHint | None = None

        presentation = error.presentation
        self.setWindowTitle(BRAND.display_name)
        self.setModal(True)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        icon_name, _severity = _SEVERITY_LOOK.get(
            presentation.severity, ("info", StatusSeverity.INFO)
        )
        heading_row = QHBoxLayout()
        heading_row.setSpacing(12)
        icon_label = QLabel()
        icon_label.setPixmap(IconSet().pixmap(icon_name, 28, "text"))
        icon_label.setFixedWidth(28)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        heading_row.addWidget(icon_label)

        title = QLabel(presentation.title)
        title.setFont(font_title())
        title.setWordWrap(True)
        title.setProperty("role", "title")
        heading_row.addWidget(title, 1)
        layout.addLayout(heading_row)

        detail = QLabel(error.detail)
        detail.setFont(font_body())
        detail.setWordWrap(True)
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(detail)

        context_text = self._describe_context()
        if context_text:
            context_label = QLabel(context_text)
            context_label.setProperty("role", "caption")
            context_label.setWordWrap(True)
            context_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            layout.addWidget(context_label)

        layout.addStretch(1)

        buttons = QDialogButtonBox()
        for hint in presentation.actions:
            label = ACTION_LABELS.get(hint)
            if not label:
                continue
            button = QPushButton(label)
            button.setAccessibleName(label)
            button.setAutoDefault(False)
            button.clicked.connect(
                lambda _checked=False, chosen=hint: self._choose(chosen)
            )
            buttons.addButton(button, QDialogButtonBox.ButtonRole.ActionRole)

        close = buttons.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        close.setDefault(True)
        close.setAccessibleName("Close")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        logger.info(
            "Showing a failure",
            extra={"code": error.code.value, "severity": presentation.severity.value},
        )

    def _describe_context(self) -> str:
        """Render the safe context values as a readable line.

        Only expected/actual style values reach here; the error taxonomy
        forbids credentials in context, and everything is redacted on its way
        to the log regardless.
        """
        interesting = {
            key: value
            for key, value in self._error.context.items()
            if key in {"expected", "actual", "current", "approved", "unexpected"}
            and value not in (None, "", [])
        }
        if not interesting:
            return ""
        parts = []
        for key, value in interesting.items():
            rendered = ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)
            parts.append(f"{key.capitalize()}: {rendered}")
        return "\n".join(parts)

    def _choose(self, hint: ActionHint) -> None:
        self._chosen = hint
        self.accept()

    @property
    def chosen_action(self) -> ActionHint | None:
        """Which suggested step the user picked, if any."""
        return self._chosen


def show_error(error: AppError, parent: QWidget | None = None) -> ActionHint | None:
    """Show ``error`` and return the action the user chose."""
    dialog = ErrorDialog(error, parent)
    dialog.exec()
    return dialog.chosen_action


def confirm(
    parent: QWidget | None,
    *,
    title: str,
    message: str,
    confirm_text: str = "Continue",
    cancel_text: str = "Cancel",
    destructive: bool = False,
    detail: str | None = None,
) -> bool:
    """Ask a yes/no question, with the safe answer as the default.

    Used for anything irreversible. ``destructive`` only changes the icon and
    which button is emphasised -- Escape and Enter always cancel either way,
    so a stray keypress cannot confirm.
    """
    box = QMessageBox(parent)
    box.setWindowTitle(BRAND.display_name)
    box.setIcon(
        QMessageBox.Icon.Warning if destructive else QMessageBox.Icon.Question
    )
    box.setText(title)
    box.setInformativeText(message)
    if detail:
        box.setDetailedText(detail)

    proceed = box.addButton(confirm_text, QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(cancel_text, QMessageBox.ButtonRole.RejectRole)
    proceed.setAccessibleName(confirm_text)
    cancel.setAccessibleName(cancel_text)
    # The safe choice is the default and the escape route.
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)

    box.exec()
    return box.clickedButton() is proceed
