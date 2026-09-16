"""The Settings screen and the dialogs that belong only to it.

Kept in its own package so the screen, its one-time consent dialog and the
small widget bridge they need can grow without the rest of the interface
importing them.
"""

from __future__ import annotations

from app.ui.settings.auto_buy_dialog import ALWAYS_CHECKED, AutoBuyConsentDialog
from app.ui.settings.settings_page import SettingsPage

__all__ = ["ALWAYS_CHECKED", "AutoBuyConsentDialog", "SettingsPage"]
