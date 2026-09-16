"""Semantic design tokens for the light and dark themes.

Token *names* describe a role, never a colour: ``danger`` rather than ``red``,
``surfaceAlt`` rather than ``grey100``. That is what makes a second theme
possible at all -- the dark theme is not "the light theme inverted", it is the
same roles with different values -- and it stops a widget author from reaching
for "the blue one" when they mean "the accent".

Values sit deliberately close to Windows 11's own palette so the application
does not read as a foreign object next to File Explorer or Settings: page
``#F3F3F3`` on a white card in light mode, ``#202020`` under ``#2B2B2B`` in
dark mode, and the system-typical accents ``#0067C0`` / ``#4CC2FF``. Note the
asymmetry in the accent: the dark accent is a *light* blue, so text on it must
be dark. That is why ``accentText`` is a token rather than a hard-coded white.

Contrast is verified, not assumed. This application displays money and refuses
purchases; a warning nobody can read is a safety defect, not a cosmetic one.
:func:`contrast_ratio` implements the WCAG 2.x relative-luminance formula and
``tests/unit/test_theme.py`` asserts every foreground/background pair defined
here clears AA (4.5:1) for body text in *both* themes.

Geometry and spacing live in :data:`_BASE` and are shared by both themes: a
theme change must never move anything, only recolour it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Mapping

#: Minimum WCAG 2.x contrast ratio for body-sized text.
AA_CONTRAST_BODY: Final[float] = 4.5

#: Minimum for large text (>=18.66px bold or >=24px). Used by the metric
#: numbers on the dashboard, which are the only oversized text we ship.
AA_CONTRAST_LARGE: Final[float] = 3.0


def _channel(value: int) -> float:
    """Linearise one 0-255 sRGB channel, per WCAG's transfer function."""
    srgb = value / 255.0
    if srgb <= 0.04045:
        return srgb / 12.92
    return ((srgb + 0.055) / 1.055) ** 2.4


def _parse_hex(colour: str) -> tuple[int, int, int]:
    """Parse ``#RGB`` or ``#RRGGBB`` into 0-255 components.

    Raises :class:`ValueError` on anything else. Tokens are authored by hand,
    so a malformed literal should fail at test time rather than silently
    render as black.
    """
    text = colour.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        raise ValueError(f"Not a hex colour: {colour!r}")
    try:
        return (
            int(text[0:2], 16),
            int(text[2:4], 16),
            int(text[4:6], 16),
        )
    except ValueError as exc:
        raise ValueError(f"Not a hex colour: {colour!r}") from exc


def relative_luminance(colour: str) -> float:
    """WCAG relative luminance of an opaque hex colour, in ``[0, 1]``."""
    red, green, blue = _parse_hex(colour)
    return (
        0.2126 * _channel(red)
        + 0.7152 * _channel(green)
        + 0.0722 * _channel(blue)
    )


def contrast_ratio(hex_a: str, hex_b: str) -> float:
    """Contrast ratio between two opaque hex colours, from 1.0 to 21.0.

    Order does not matter. Black against white is exactly 21.0, which is the
    cheapest available sanity check on this implementation.
    """
    lum_a = relative_luminance(hex_a)
    lum_b = relative_luminance(hex_b)
    lighter, darker = max(lum_a, lum_b), min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


# ---------------------------------------------------------------------------
# Geometry and spacing (theme-independent)
# ---------------------------------------------------------------------------

#: Shared non-colour tokens. Values are QSS length strings because they are
#: substituted straight into the stylesheet.
#:
#: Radii are small on purpose: Windows 11 uses 4px on controls and 8px on
#: surfaces, and anything rounder reads as a web page rather than a utility.
#: There are no font sizes here -- see :mod:`app.ui.theme.fonts` for why.
_BASE: Final[Mapping[str, str]] = {
    "radiusSm": "4px",
    "radius": "6px",
    "radiusLg": "8px",
    "spaceXs": "4px",
    "spaceSm": "8px",
    "spaceMd": "12px",
    "spaceLg": "16px",
    "spaceXl": "24px",
    "strokeWidth": "1px",
}


# ---------------------------------------------------------------------------
# Light theme
# ---------------------------------------------------------------------------

LIGHT_TOKENS: Final[Mapping[str, str]] = {
    **_BASE,
    # Surfaces, from furthest back to nearest front.
    "bg": "#F3F3F3",
    "surface": "#FFFFFF",
    "surfaceAlt": "#FAFAFA",
    "surfaceHover": "#F5F5F5",
    "stroke": "#E5E5E5",
    "strokeStrong": "#D1D1D1",
    # Text. ``textDisabled`` is exempt from the AA rule by WCAG itself
    # (disabled controls), but is kept legible enough to read the label.
    "text": "#1B1B1B",
    "textMuted": "#616161",
    "textDisabled": "#9D9D9D",
    # Primary action.
    "accent": "#0067C0",
    "accentHover": "#0078D4",
    "accentPressed": "#005A9E",
    "accentText": "#FFFFFF",
    # Status expression colours: used as badge/dot foregrounds on their own
    # tinted background and directly on a card surface.
    "danger": "#8E1519",
    "dangerBg": "#FDE7E9",
    "success": "#0F5132",
    "successBg": "#DFF6E5",
    "warning": "#6E4A00",
    "warningBg": "#FFF4CE",
    "info": "#114A8C",
    "infoBg": "#E5F1FB",
    # "Blocked" is its own colour, not a shade of danger: a blocked purchase
    # is the guard working correctly, and must not look like a crash.
    "blocked": "#6B2D8F",
    "blockedBg": "#F3E8FB",
    "neutral": "#444444",
    "neutralBg": "#EDEDED",
    # Destructive *filled* button. Separate from ``danger`` because that token
    # is a foreground; one value cannot be both a legible fill and a legible
    # text colour in both themes.
    "dangerFill": "#C42B1C",
    "dangerFillHover": "#B2261A",
    "dangerFillPressed": "#9A2017",
    "dangerFillText": "#FFFFFF",
    # Focus ring. Windows 11 draws a dark ring in light mode (and light in
    # dark mode) rather than an accent-coloured one.
    "focusRing": "#1B1B1B",
}


# ---------------------------------------------------------------------------
# Dark theme
# ---------------------------------------------------------------------------

DARK_TOKENS: Final[Mapping[str, str]] = {
    **_BASE,
    "bg": "#202020",
    "surface": "#2B2B2B",
    "surfaceAlt": "#323232",
    "surfaceHover": "#383838",
    "stroke": "#393939",
    "strokeStrong": "#4A4A4A",
    "text": "#FFFFFF",
    "textMuted": "#C5C5C5",
    "textDisabled": "#7A7A7A",
    # The dark accent is light, so its text is dark. Widgets that read
    # ``accentText`` instead of assuming white are the reason this works.
    "accent": "#4CC2FF",
    "accentHover": "#65CCFF",
    "accentPressed": "#3AA9E0",
    "accentText": "#061A24",
    "danger": "#FF99A4",
    "dangerBg": "#2B1B1E",
    "success": "#6CCB5F",
    "successBg": "#1E2A1F",
    "warning": "#FCE100",
    "warningBg": "#2C2A15",
    "info": "#76C7FF",
    "infoBg": "#18242D",
    "blocked": "#D29BF0",
    "blockedBg": "#2A1F33",
    "neutral": "#C5C5C5",
    "neutralBg": "#333333",
    # The filled red stays saturated in dark mode: a lighter red would lose
    # the "stop" reading, and white-on-#C42B1C is 5.66:1 either way.
    "dangerFill": "#C42B1C",
    "dangerFillHover": "#D13A2A",
    "dangerFillPressed": "#A82418",
    "dangerFillText": "#FFFFFF",
    "focusRing": "#FFFFFF",
}


# ---------------------------------------------------------------------------
# Status semantics
# ---------------------------------------------------------------------------


class Semantic(StrEnum):
    """A status meaning, and the token pair that expresses it.

    Badges, status dots and guard rows take a :class:`Semantic` rather than
    two colours, so "blocked" looks the same everywhere it appears. The values
    match the ``severity`` dynamic property the stylesheet selects on, and the
    members of :class:`app.ui.theme.theme.StatusSeverity`.
    """

    SUCCESS = "success"
    WARNING = "warning"
    BLOCKED = "blocked"
    ERROR = "error"
    INFO = "info"
    NEUTRAL = "neutral"

    @property
    def label(self) -> str:
        """Plain-English word for the badge text when no specific text fits."""
        return {
            Semantic.SUCCESS: "Done",
            Semantic.WARNING: "Attention",
            Semantic.BLOCKED: "Blocked",
            Semantic.ERROR: "Problem",
            Semantic.INFO: "Info",
            Semantic.NEUTRAL: "Idle",
        }[self]

    @property
    def tokens(self) -> tuple[str, str]:
        """``(foreground_token, background_token)`` for this status.

        ``ERROR`` maps to the ``danger`` pair: the taxonomy in
        :mod:`app.core.errors` calls it an error, the palette calls the colour
        danger, and this property is the single place the two names meet.
        """
        return _SEMANTIC_TOKENS[self]

    def colours(self, tokens: Mapping[str, str]) -> tuple[str, str]:
        """Resolve :attr:`tokens` against a token set to ``(fg, bg)`` hexes."""
        foreground, background = self.tokens
        return tokens[foreground], tokens[background]


_SEMANTIC_TOKENS: Final[Mapping[Semantic, tuple[str, str]]] = {
    Semantic.SUCCESS: ("success", "successBg"),
    Semantic.WARNING: ("warning", "warningBg"),
    Semantic.BLOCKED: ("blocked", "blockedBg"),
    Semantic.ERROR: ("danger", "dangerBg"),
    Semantic.INFO: ("info", "infoBg"),
    Semantic.NEUTRAL: ("neutral", "neutralBg"),
}


def tokens_for(dark: bool) -> Mapping[str, str]:
    """The token set for the requested appearance."""
    return DARK_TOKENS if dark else LIGHT_TOKENS
