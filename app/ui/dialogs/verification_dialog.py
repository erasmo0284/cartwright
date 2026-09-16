"""Handing control to the user when Amazon asks for a human.

The application does not solve image challenges, enter one-time codes or
pretend not to be automation. When Amazon wants a person, it gets one: the
browser window comes forward, this dialog explains what to do, and the run
continues once the check is cleared.

The dialog is deliberately non-modal to the browser -- it stays on screen
while the user works in the browser window -- and it closes itself as soon as
the automation reports that the page has moved on.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout, QWidget

from app.branding import BRAND
from app.ui.components import ButtonRow, PrimaryButton, ProgressStrip, SubtleButton
from app.ui.theme import StatusSeverity, font_body, font_title
from app.ui.components import StatusBadge

logger = logging.getLogger("app.ui.dialogs.verification")


class VerificationDialog(QDialog):
    """Tells the user that Amazon needs them, and waits."""

    #: The user asked to bring the browser window forward again.
    show_browser_requested = Signal()
    #: The user gave up.
    cancelled = Signal()

    def __init__(
        self,
        *,
        reason: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Amazon needs your attention - {BRAND.display_name}")
        self.setModal(False)
        self.setMinimumWidth(460)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        heading = QLabel("Amazon needs your attention")
        heading.setFont(font_title())
        heading.setProperty("role", "title")
        layout.addWidget(heading)

        badge = StatusBadge()
        badge.set_status("Nothing has been ordered", StatusSeverity.INFO)
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignLeft)

        body = QLabel(
            reason
            or (
                "Amazon is asking for a security check, such as a one-time "
                "code or an image challenge. Complete it in the browser "
                "window and this will carry on by itself."
            )
        )
        body.setFont(font_body())
        body.setWordWrap(True)
        layout.addWidget(body)

        privacy = QLabel(
            "This app never sees your password or your codes. You are signing "
            "in directly with Amazon."
        )
        privacy.setProperty("role", "caption")
        privacy.setWordWrap(True)
        layout.addWidget(privacy)

        self._progress = ProgressStrip()
        self._progress.start("Waiting for you to finish...")
        layout.addWidget(self._progress)

        show_browser = PrimaryButton("Show the browser")
        show_browser.setDefault(True)
        show_browser.clicked.connect(self.show_browser_requested)

        stop = SubtleButton("Stop and try later")
        stop.clicked.connect(self._on_cancel)

        layout.addWidget(ButtonRow(stop, show_browser, align="right"))
        logger.info("Showing the verification handoff")

    def set_message(self, message: str) -> None:
        """Update the waiting message as the automation reports progress."""
        self._progress.set_message(message)

    def mark_cleared(self) -> None:
        """Called when the challenge has been completed. Closes the dialog."""
        self._progress.stop()
        logger.info("Verification cleared; closing the handoff dialog")
        self.accept()

    def _on_cancel(self) -> None:
        self._progress.stop()
        self.cancelled.emit()
        self.reject()

    def closeEvent(self, event: object) -> None:  # noqa: N802 - Qt naming
        self._progress.stop()
        super().closeEvent(event)  # type: ignore[arg-type]
