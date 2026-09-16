"""Dialogs.

The dialogs in this package carry most of the application's safety story,
because they are where a person is asked to approve spending money, told that
something was blocked, or asked what happened to an order whose outcome could
not be read. Their wording is part of the product, not decoration.

Shared rules, applied throughout:

* The destructive or money-spending action is **never** the default button.
  Enter and Escape always take the safe path.
* A refusal always says what was expected, what was found, and that no order
  was placed.
* No dialog ever shows a state name, an error code or an underscore.
"""

from __future__ import annotations

from app.ui.dialogs.about_dialog import AboutDialog
from app.ui.dialogs.blocked_dialog import BlockedDialog
from app.ui.dialogs.cart_permission_dialog import CartPermissionDialog
from app.ui.dialogs.confirm_purchase_dialog import ConfirmPurchaseDialog
from app.ui.dialogs.error_dialog import (
    ErrorDialog,
    confirm,
    confirm_destructive,
    show_error,
)
from app.ui.dialogs.test_result_dialog import TestResultDialog
from app.ui.dialogs.uncertain_order_dialog import UncertainOrderDialog
from app.ui.dialogs.verification_dialog import VerificationDialog

__all__ = [
    "AboutDialog",
    "BlockedDialog",
    "CartPermissionDialog",
    "ConfirmPurchaseDialog",
    "ErrorDialog",
    "TestResultDialog",
    "UncertainOrderDialog",
    "VerificationDialog",
    "confirm",
    "confirm_destructive",
    "show_error",
]
