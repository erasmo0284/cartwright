"""The visual theme: tokens, stylesheet, fonts, icons and the theme manager.

Everything the application needs to *look* like a Windows 11 utility lives
here, and nothing here knows anything about the product's behaviour. Widgets
ask for a token, a font or an icon; they never write a colour literal.

The design rule that shapes this whole package: a widget that is styled with
QSS is re-routed through ``QStyleSheetStyle`` and loses the native
``windows11`` drawing (hover and press animations, the correct control
geometry and the system accent). So the stylesheet in :mod:`.stylesheet`
touches only surfaces this application invents -- cards, badges, the
navigation list, the banner -- addressed by ``objectName`` and dynamic
properties, and leaves every standard control alone.

Typical use at startup::

    theme = ThemeManager(app)
    theme.set_mode(ThemeMode.SYSTEM)
    theme.apply_window_chrome(main_window)
"""

from __future__ import annotations

from app.ui.theme.fonts import (
    font_body,
    font_caption,
    font_metric,
    font_section,
    font_title,
    ui_font,
)
from app.ui.theme.icons import IconSet, app_icon, tray_icon, write_ico
from app.ui.theme.stylesheet import build_qss
from app.ui.theme.theme import StatusSeverity, ThemeManager, ThemeMode
from app.ui.theme.tokens import (
    DARK_TOKENS,
    LIGHT_TOKENS,
    Semantic,
    contrast_ratio,
)

__all__ = [
    "DARK_TOKENS",
    "LIGHT_TOKENS",
    "IconSet",
    "Semantic",
    "StatusSeverity",
    "ThemeManager",
    "ThemeMode",
    "app_icon",
    "build_qss",
    "contrast_ratio",
    "font_body",
    "font_caption",
    "font_metric",
    "font_section",
    "font_title",
    "tray_icon",
    "ui_font",
    "write_ico",
]
