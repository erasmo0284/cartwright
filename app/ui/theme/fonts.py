"""Typography.

Windows 11 ships *Segoe UI Variable* in three optical sizes -- Small, Text and
Display -- and the shell uses them by role: Small for 12px-and-under captions,
Text for body copy, Display for headings. Win32's system font metric still
reports plain "Segoe UI", so a Qt application that simply takes the default
font is one notch off the platform it is running on: slightly loose captions
and slightly weak headings. Choosing the optical size per role is the cheapest
change that makes the window look like it belongs to the OS.

Each family is checked against :meth:`QFontDatabase.families` before use, so
the same code degrades in order to ``Segoe UI Variable`` (non-optical),
``Segoe UI`` (Windows 10) and finally whatever Qt picked, which is what
happens on the offscreen platform in CI.

Sizes are **point sizes**, never pixels: Qt scales points by the display's
logical DPI, so text grows with the user's 125% / 150% scale setting. This is
also why :mod:`app.ui.theme.stylesheet` sets no ``font-size`` at all -- a QSS
pixel size would stay fixed while its container grew.
"""

from __future__ import annotations

from typing import Final, Mapping

from PySide6.QtGui import QFont, QFontDatabase

#: Point sizes for the named roles. Body is 9pt, which is Windows' own
#: 12px-at-96dpi UI size; the rest are derived from it so the hierarchy holds
#: at any scale factor.
BODY_POINT_SIZE: Final[float] = 9.0
CAPTION_POINT_SIZE: Final[float] = 8.25
SECTION_POINT_SIZE: Final[float] = 10.5
TITLE_POINT_SIZE: Final[float] = 13.5
METRIC_POINT_SIZE: Final[float] = 24.0

#: Role -> preferred optical size. ``caption`` takes Small because that is the
#: size Segoe UI Variable Small was cut for; ``title`` and ``metric`` take
#: Display, whose tighter spacing is what makes a heading read as a heading.
_ROLE_OPTICAL: Final[Mapping[str, str]] = {
    "caption": "Segoe UI Variable Small",
    "text": "Segoe UI Variable Text",
    "body": "Segoe UI Variable Text",
    "section": "Segoe UI Variable Text",
    "title": "Segoe UI Variable Display",
    "metric": "Segoe UI Variable Display",
}

#: Tried after the role's optical family, in order.
_FALLBACKS: Final[tuple[str, ...]] = (
    "Segoe UI Variable",
    "Segoe UI",
)

#: Every family this module may ever return, for assertions in tests.
FONT_CANDIDATES: Final[tuple[str, ...]] = (
    "Segoe UI Variable Small",
    "Segoe UI Variable Text",
    "Segoe UI Variable Display",
    *_FALLBACKS,
)


def _installed_families() -> frozenset[str]:
    """Installed font families.

    Not cached: fonts can be added at runtime, and this is called a handful
    of times per theme change, not per paint.
    """
    return frozenset(QFontDatabase.families())


def _resolve_family(role: str) -> str | None:
    """The best available family for ``role``, or ``None`` for Qt's default.

    An unknown role falls back to the body optical size rather than raising:
    a mistyped role should cost a little polish, not crash a window while it
    is being built.
    """
    preferred = _ROLE_OPTICAL.get(role, _ROLE_OPTICAL["text"])
    installed = _installed_families()
    for family in (preferred, *_FALLBACKS):
        if family in installed:
            return family
    return None


def ui_font(
    point_size: float = BODY_POINT_SIZE,
    role: str = "text",
    weight: QFont.Weight | None = None,
) -> QFont:
    """A font for ``role`` at ``point_size``.

    ``point_size`` is preserved exactly (via ``setPointSizeF``), so callers
    can use fractional sizes such as 8.25pt without them being rounded into a
    different step of the type scale.
    """
    font = QFont()
    family = _resolve_family(role)
    if family is not None:
        font.setFamily(family)
    font.setPointSizeF(point_size)
    if weight is not None:
        font.setWeight(weight)
    # Windows 11's shell text is hinted lightly; full hinting makes the
    # variable font's stems snap to pixels and look uneven at 125%.
    font.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
    return font


def font_body() -> QFont:
    """Body copy: list rows, form labels, paragraphs."""
    return ui_font(BODY_POINT_SIZE, role="body")


def font_caption() -> QFont:
    """Secondary detail: timestamps, helper text under a field."""
    return ui_font(CAPTION_POINT_SIZE, role="caption")


def font_section() -> QFont:
    """A section heading inside a card."""
    return ui_font(SECTION_POINT_SIZE, role="section", weight=QFont.Weight.DemiBold)


def font_title() -> QFont:
    """A page or card title."""
    return ui_font(TITLE_POINT_SIZE, role="title", weight=QFont.Weight.DemiBold)


def font_metric() -> QFont:
    """The large numbers on the dashboard (price, count, saving).

    DemiBold rather than Bold: at 24pt a bold variable face turns heavy
    enough to read as an alert, which is not what a count of watched items
    should say.
    """
    return ui_font(METRIC_POINT_SIZE, role="metric", weight=QFont.Weight.DemiBold)
