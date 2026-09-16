"""The one-time consent for automatic purchasing.

Automatic purchasing is the only feature in this application that spends the
user's money without asking again, so it is the only one behind a dialog like
this. The dialog has three jobs, in this order:

1. Say plainly what changes: real orders, placed without a further question.
2. Say what does *not* change -- the list of checks that still has to pass
   before any order is placed. A user who is nervous about this feature is
   nervous about the program buying the wrong thing, and the honest answer is
   the list.
3. Make agreeing a separate act from reading. The confirm button stays
   disabled until the "I understand" box is ticked, and Cancel is the default
   button, so no amount of pressing Return turns this on.

The dialog only collects consent. It does not write any setting; the window
that opened it records the result.
"""

from __future__ import annotations

import logging
from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.branding import BRAND
from app.ui.components import DangerButton, HLine, role_label

logger = logging.getLogger("app.ui.settings")

#: Everything that must still be true before an order is placed, in the order
#: the purchase checks run. This is the promise the dialog makes, so it is a
#: named list rather than a paragraph: it is read aloud by screen readers one
#: line at a time, and it can be compared against the real checks.
ALWAYS_CHECKED: Final[tuple[str, ...]] = (
    "The product is the exact one you chose",
    "The version, size or colour is the one you chose",
    "The quantity is the one you set",
    "The seller is one your rules allow",
    "The condition is one your rules allow",
    "The item price is at or below your price limit",
    "The order total is at or below your total limit",
    "The delivery address is the one you approved",
    "The payment method is the one you approved",
    "Nothing extra has been added to the order",
)

_HEADING: Final = "Turn on automatic purchasing"

_EXPLANATION: Final = (
    "With automatic purchasing on, {app} places a real order for you as soon "
    "as a product meets your conditions. It will not ask you first, and it "
    "may place that order while you are away from your computer. Real money "
    "is spent."
)

_STILL_CHECKED_HEADING: Final = "Every one of these is still checked first"

_TEST_MODE_NOTE: Final = (
    "Test mode must also be off before any order can be placed. While test "
    "mode is on, the program stops at the last step and orders nothing."
)

_UNDERSTAND: Final = (
    "I understand that real orders will be placed without asking me each time"
)


class AutoBuyConsentDialog(QDialog):
    """Asks the user to accept what automatic purchasing means."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_HEADING)
        self.setModal(True)
        #: ``True`` only once the user has ticked the box and confirmed.
        self.accepted_consent = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)

        heading = role_label(_HEADING, "title", self)
        heading.setWordWrap(True)
        layout.addWidget(heading)

        explanation = role_label(
            _EXPLANATION.format(app=BRAND.display_name), None, self
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        layout.addWidget(HLine(self))

        checks_heading = role_label(_STILL_CHECKED_HEADING, "sectionHeading", self)
        checks_heading.setWordWrap(True)
        layout.addWidget(checks_heading)

        # One label per item rather than a rich-text bullet list, so each line
        # is its own accessible object and can be read out on its own.
        for item in ALWAYS_CHECKED:
            line = role_label(f"•  {item}", None, self)
            line.setWordWrap(True)
            line.setAccessibleName(item)
            layout.addWidget(line)

        note = role_label(_TEST_MODE_NOTE, "caption", self)
        note.setWordWrap(True)
        layout.addWidget(note)

        layout.addWidget(HLine(self))

        self.understand_check = QCheckBox(_UNDERSTAND, self)
        self.understand_check.setAccessibleName(_UNDERSTAND)
        self.understand_check.toggled.connect(self._on_understood)
        layout.addWidget(self.understand_check)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(8)
        buttons.addStretch(1)

        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.setAccessibleName("Leave automatic purchasing off")
        self.cancel_button.setDefault(True)
        self.cancel_button.setAutoDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)

        self.confirm_button = DangerButton(_HEADING, self)
        self.confirm_button.setAccessibleName(_HEADING)
        self.confirm_button.setEnabled(False)
        self.confirm_button.setAutoDefault(False)
        self.confirm_button.clicked.connect(self._on_confirmed)
        buttons.addWidget(self.confirm_button)

        layout.addLayout(buttons)

        self.cancel_button.setFocus(Qt.FocusReason.OtherFocusReason)

    # ---- internals -------------------------------------------------------

    def _on_understood(self, understood: bool) -> None:
        self.confirm_button.setEnabled(understood)

    def _on_confirmed(self) -> None:
        """Record consent and close. Guarded in case the box was unticked."""
        if not self.understand_check.isChecked():
            return
        self.accepted_consent = True
        logger.info("The user turned on automatic purchasing")
        self.accept()
