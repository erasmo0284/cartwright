"""Tests for the visual theme layer.

Two of these tests exist to catch defects that would otherwise reach a user
with no error message at all:

* a token typo in the QSS template makes Qt discard the declaration silently,
  so the placeholder set is checked against the token keys;
* an unreadable status colour is a safety problem in an application that shows
  money and refuses purchases, so every foreground/background pair is held to
  WCAG AA.

Everything Qt-related runs under the offscreen platform. The environment
variable is set before the first :class:`QApplication` is constructed, because
Qt reads it once at that point; nothing may open a window during a test run.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterator, Mapping

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402  (must follow the platform setting)
from PySide6.QtGui import QFont, QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.theme import fonts, icons  # noqa: E402
from app.ui.theme.stylesheet import build_qss, template_source  # noqa: E402
from app.ui.theme.theme import StatusSeverity, ThemeManager, ThemeMode  # noqa: E402
from app.ui.theme.tokens import (  # noqa: E402
    AA_CONTRAST_BODY,
    DARK_TOKENS,
    LIGHT_TOKENS,
    Semantic,
    contrast_ratio,
)

TOKEN_SETS: Mapping[str, Mapping[str, str]] = {
    "light": LIGHT_TOKENS,
    "dark": DARK_TOKENS,
}

_PLACEHOLDER = re.compile(r"\$\{(\w+)\}")


@pytest.fixture(scope="module")
def qt_app() -> Iterator[QApplication]:
    """A single offscreen :class:`QApplication` for the whole module.

    Qt permits only one application object per process, so this is reused
    rather than recreated, and it is never destroyed: tearing it down while
    other Qt objects are alive crashes the interpreter.
    """
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    yield app


# ---------------------------------------------------------------------------
# contrast_ratio
# ---------------------------------------------------------------------------


def test_contrast_ratio_black_on_white_is_maximum() -> None:
    assert contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0, abs=0.01)


def test_contrast_ratio_is_symmetric_and_identity_is_one() -> None:
    assert contrast_ratio("#FFFFFF", "#000000") == pytest.approx(21.0, abs=0.01)
    assert contrast_ratio("#0067C0", "#0067C0") == pytest.approx(1.0, abs=0.001)


def test_contrast_ratio_known_midpoint() -> None:
    # Mid grey against white is a standard reference value (4.61:1).
    assert contrast_ratio("#767676", "#FFFFFF") == pytest.approx(4.54, abs=0.05)


def test_contrast_ratio_accepts_short_hex() -> None:
    assert contrast_ratio("#000", "#fff") == pytest.approx(21.0, abs=0.01)


def test_contrast_ratio_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        contrast_ratio("teal", "#FFFFFF")


# ---------------------------------------------------------------------------
# Accessibility of the palettes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("theme", sorted(TOKEN_SETS))
@pytest.mark.parametrize("accent_key", ["accent", "accentHover", "accentPressed"])
def test_accent_text_is_readable_on_the_accent(theme: str, accent_key: str) -> None:
    tokens = TOKEN_SETS[theme]
    ratio = contrast_ratio(tokens["accentText"], tokens[accent_key])
    assert ratio >= AA_CONTRAST_BODY, f"{theme} {accent_key} = {ratio:.2f}:1"


@pytest.mark.parametrize("theme", sorted(TOKEN_SETS))
@pytest.mark.parametrize("semantic", list(Semantic))
def test_status_foreground_is_readable_on_its_background(
    theme: str, semantic: Semantic
) -> None:
    foreground, background = semantic.colours(TOKEN_SETS[theme])
    ratio = contrast_ratio(foreground, background)
    assert ratio >= AA_CONTRAST_BODY, (
        f"{theme} {semantic.value}: {foreground} on {background} = {ratio:.2f}:1"
    )


@pytest.mark.parametrize("theme", sorted(TOKEN_SETS))
@pytest.mark.parametrize("semantic", list(Semantic))
def test_status_foreground_is_readable_on_a_card(
    theme: str, semantic: Semantic
) -> None:
    """Status text is also drawn plainly on a card, not only inside a badge."""
    tokens = TOKEN_SETS[theme]
    foreground, _ = semantic.colours(tokens)
    ratio = contrast_ratio(foreground, tokens["surface"])
    assert ratio >= AA_CONTRAST_BODY, f"{theme} {semantic.value} = {ratio:.2f}:1"


@pytest.mark.parametrize("theme", sorted(TOKEN_SETS))
@pytest.mark.parametrize("text_key", ["text", "textMuted"])
@pytest.mark.parametrize("surface_key", ["bg", "surface", "surfaceAlt", "surfaceHover"])
def test_body_text_is_readable_on_every_surface(
    theme: str, text_key: str, surface_key: str
) -> None:
    tokens = TOKEN_SETS[theme]
    ratio = contrast_ratio(tokens[text_key], tokens[surface_key])
    assert ratio >= AA_CONTRAST_BODY, (
        f"{theme} {text_key} on {surface_key} = {ratio:.2f}:1"
    )


@pytest.mark.parametrize("theme", sorted(TOKEN_SETS))
def test_danger_button_label_is_readable(theme: str) -> None:
    tokens = TOKEN_SETS[theme]
    for fill in ("dangerFill", "dangerFillHover", "dangerFillPressed"):
        ratio = contrast_ratio(tokens["dangerFillText"], tokens[fill])
        assert ratio >= AA_CONTRAST_BODY, f"{theme} {fill} = {ratio:.2f}:1"


# ---------------------------------------------------------------------------
# Token set shape
# ---------------------------------------------------------------------------


def test_both_token_sets_define_the_same_keys() -> None:
    assert set(LIGHT_TOKENS) == set(DARK_TOKENS)


def test_required_token_keys_are_present() -> None:
    required = {
        "bg",
        "surface",
        "surfaceAlt",
        "surfaceHover",
        "stroke",
        "strokeStrong",
        "text",
        "textMuted",
        "textDisabled",
        "accent",
        "accentHover",
        "accentPressed",
        "accentText",
        "danger",
        "dangerBg",
        "success",
        "successBg",
        "warning",
        "warningBg",
        "info",
        "infoBg",
        "blocked",
        "blockedBg",
        "focusRing",
        "radius",
        "radiusSm",
        "radiusLg",
        "spaceXs",
        "spaceSm",
        "spaceMd",
        "spaceLg",
        "spaceXl",
        "strokeWidth",
    }
    for name, tokens in TOKEN_SETS.items():
        assert required <= set(tokens), f"{name} is missing {required - set(tokens)}"


def test_geometry_tokens_are_identical_across_themes() -> None:
    """A theme change recolours; it must never move anything."""
    for key in ("radius", "radiusSm", "radiusLg", "spaceMd", "strokeWidth"):
        assert LIGHT_TOKENS[key] == DARK_TOKENS[key]


def test_semantic_maps_every_member_to_a_real_token_pair() -> None:
    for semantic in Semantic:
        foreground, background = semantic.tokens
        assert foreground in LIGHT_TOKENS
        assert background in LIGHT_TOKENS
        assert semantic.label


def test_semantic_values_match_status_severity_values() -> None:
    """The stylesheet selects on one set of strings; both enums must use it."""
    assert {member.value for member in Semantic} == {
        member.value for member in StatusSeverity
    }


# ---------------------------------------------------------------------------
# build_qss
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("theme", sorted(TOKEN_SETS))
def test_build_qss_substitutes_every_placeholder(theme: str) -> None:
    qss = build_qss(TOKEN_SETS[theme])
    assert qss.strip()
    assert "${" not in qss
    assert "$" not in qss


@pytest.mark.parametrize("theme", sorted(TOKEN_SETS))
def test_build_qss_emits_the_theme_colours(theme: str) -> None:
    tokens = TOKEN_SETS[theme]
    qss = build_qss(tokens)
    assert tokens["surface"] in qss
    assert tokens["accent"] in qss


def test_build_qss_raises_on_a_missing_token() -> None:
    incomplete = dict(LIGHT_TOKENS)
    del incomplete["accent"]
    with pytest.raises(KeyError):
        build_qss(incomplete)


def test_build_qss_raises_on_an_almost_empty_mapping() -> None:
    with pytest.raises(KeyError):
        build_qss({"bg": "#FFFFFF"})


@pytest.mark.parametrize("theme", sorted(TOKEN_SETS))
def test_template_references_no_unknown_token(theme: str) -> None:
    placeholders = set(_PLACEHOLDER.findall(template_source()))
    assert placeholders, "the regex should find the template's placeholders"
    unknown = placeholders - set(TOKEN_SETS[theme])
    assert not unknown, f"{theme} has no value for {sorted(unknown)}"


def test_template_sets_no_pixel_font_size() -> None:
    """Pixel font sizes do not scale with the display scale factor."""
    assert not re.search(r"font-size", template_source())


def test_template_leaves_standard_controls_native() -> None:
    """Blanket rules on these would cost native Windows 11 rendering.

    Comments are stripped first: several of them name the very classes this
    test forbids, precisely to explain why they are left alone.
    """
    rules = re.sub(r"/\*.*?\*/", "", template_source(), flags=re.DOTALL)
    for selector in (
        "QPushButton",
        "QComboBox",
        "QCheckBox",
        "QSpinBox",
        "QScrollBar",
        "QLineEdit",
    ):
        assert selector not in rules, f"{selector} must stay native"
    # The paste field is styled by objectName instead.
    assert "#UrlInput" in rules


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------


def _acceptable_family(family: str) -> bool:
    """True for a Segoe candidate, or for Qt's own default.

    The offscreen platform used by this suite has no access to the Windows
    font database, so ``ui_font`` correctly falls through to the Qt default
    there. Accepting that is what keeps this test meaningful on Windows
    without making it fail in CI.
    """
    return family in fonts.FONT_CANDIDATES or family == QFont().family()


def test_ui_font_family_is_an_expected_candidate(qt_app: QApplication) -> None:
    assert _acceptable_family(fonts.ui_font().family())


@pytest.mark.parametrize("role", ["caption", "text", "body", "section", "title", "metric"])
def test_every_role_resolves_to_an_acceptable_family(
    qt_app: QApplication, role: str
) -> None:
    assert _acceptable_family(fonts.ui_font(role=role).family())


def test_ui_font_preserves_the_point_size(qt_app: QApplication) -> None:
    font = fonts.ui_font(11.5)
    assert font.pointSizeF() == pytest.approx(11.5)


def test_ui_font_applies_an_explicit_weight(qt_app: QApplication) -> None:
    font = fonts.ui_font(9.0, weight=QFont.Weight.Bold)
    assert font.weight() == QFont.Weight.Bold


def test_ui_font_tolerates_an_unknown_role(qt_app: QApplication) -> None:
    font = fonts.ui_font(9.0, role="not-a-role")
    assert _acceptable_family(font.family())


@pytest.mark.parametrize(
    ("factory", "expected_size"),
    [
        (fonts.font_body, fonts.BODY_POINT_SIZE),
        (fonts.font_caption, fonts.CAPTION_POINT_SIZE),
        (fonts.font_section, fonts.SECTION_POINT_SIZE),
        (fonts.font_title, fonts.TITLE_POINT_SIZE),
        (fonts.font_metric, fonts.METRIC_POINT_SIZE),
    ],
)
def test_named_fonts_use_their_scale_step(
    qt_app: QApplication, factory, expected_size: float
) -> None:
    font = factory()
    assert font.pointSizeF() == pytest.approx(expected_size)


def test_the_type_scale_is_strictly_increasing() -> None:
    sizes = [
        fonts.CAPTION_POINT_SIZE,
        fonts.BODY_POINT_SIZE,
        fonts.SECTION_POINT_SIZE,
        fonts.TITLE_POINT_SIZE,
        fonts.METRIC_POINT_SIZE,
    ]
    assert sizes == sorted(sizes)
    assert len(set(sizes)) == len(sizes)


# ---------------------------------------------------------------------------
# ThemeManager
# ---------------------------------------------------------------------------


@pytest.fixture
def theme_manager(qt_app: QApplication) -> Iterator[ThemeManager]:
    """A manager that restores the application's styling afterwards."""
    manager = ThemeManager(qt_app)
    yield manager
    manager.set_mode(ThemeMode.SYSTEM)
    qt_app.styleHints().setColorScheme(Qt.ColorScheme.Unknown)
    qt_app.setStyleSheet("")


def test_theme_manager_applies_a_stylesheet_on_construction(
    theme_manager: ThemeManager, qt_app: QApplication
) -> None:
    assert qt_app.styleSheet().strip()


@pytest.mark.parametrize("mode", list(ThemeMode))
def test_set_mode_applies_a_stylesheet_and_emits(
    theme_manager: ThemeManager, qt_app: QApplication, mode: ThemeMode
) -> None:
    seen: list[str] = []
    theme_manager.theme_changed.connect(seen.append)
    theme_manager.set_mode(mode)

    assert seen == ["dark" if theme_manager.is_dark() else "light"]
    qss = qt_app.styleSheet()
    assert qss.strip()
    assert "${" not in qss
    assert theme_manager.mode is mode


@pytest.mark.parametrize(
    ("mode", "dark"),
    [(ThemeMode.LIGHT, False), (ThemeMode.DARK, True)],
)
def test_is_dark_agrees_with_an_explicit_mode(
    theme_manager: ThemeManager, mode: ThemeMode, dark: bool
) -> None:
    theme_manager.set_mode(mode)
    assert theme_manager.is_dark() is dark
    expected = DARK_TOKENS if dark else LIGHT_TOKENS
    assert theme_manager.current_tokens() is expected


def test_set_mode_produces_different_stylesheets_per_appearance(
    theme_manager: ThemeManager, qt_app: QApplication
) -> None:
    theme_manager.set_mode(ThemeMode.LIGHT)
    light = qt_app.styleSheet()
    theme_manager.set_mode(ThemeMode.DARK)
    dark = qt_app.styleSheet()
    assert light != dark
    assert LIGHT_TOKENS["surface"] in light
    assert DARK_TOKENS["surface"] in dark


def test_theme_mode_labels_are_present() -> None:
    assert {mode.label for mode in ThemeMode} == {"Match Windows", "Light", "Dark"}


def test_status_severity_labels_are_present() -> None:
    for severity in StatusSeverity:
        assert severity.label


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------

_REQUIRED_ICON_NAMES = (
    "dashboard",
    "purchase",
    "watchlist",
    "activity",
    "settings",
    "amazon_account",
    "check",
    "cross",
    "warning",
    "pause",
    "play",
    "refresh",
    "external",
    "trash",
    "edit",
    "clock",
    "cart",
    "shield",
    "bell",
    "info",
)


def _distinct_colours(icon: QIcon, size: int) -> int:
    pixmap = icon.pixmap(size, size)
    image = pixmap.toImage()
    return len({image.pixel(x, y) for x in range(size) for y in range(size)})


def test_icon_set_declares_every_required_name(qt_app: QApplication) -> None:
    available = set(icons.IconSet().names)
    missing = set(_REQUIRED_ICON_NAMES) - available
    assert not missing, f"missing glyphs: {sorted(missing)}"


@pytest.mark.parametrize("theme", sorted(TOKEN_SETS))
@pytest.mark.parametrize("name", _REQUIRED_ICON_NAMES)
def test_every_icon_renders_something(
    qt_app: QApplication, theme: str, name: str
) -> None:
    icon_set = icons.IconSet(TOKEN_SETS[theme])
    icon = icon_set.icon(name)
    assert not icon.isNull()
    assert icon.availableSizes()
    # More than one colour means ink actually landed on the transparent square.
    assert _distinct_colours(icon, 24) > 1, f"{name} rendered blank"


def test_icon_set_caches_repeated_requests(qt_app: QApplication) -> None:
    icon_set = icons.IconSet(LIGHT_TOKENS)
    assert icon_set.icon("check") is icon_set.icon("check")


def test_icon_set_rejects_an_unknown_name(qt_app: QApplication) -> None:
    with pytest.raises(KeyError):
        icons.IconSet(LIGHT_TOKENS).icon("nonexistent")


def test_icons_are_recoloured_per_theme(qt_app: QApplication) -> None:
    light = icons.IconSet(LIGHT_TOKENS).svg("check", LIGHT_TOKENS["text"])
    dark = icons.IconSet(DARK_TOKENS).svg("check", DARK_TOKENS["text"])
    assert LIGHT_TOKENS["text"] in light
    assert DARK_TOKENS["text"] in dark
    assert light != dark


@pytest.mark.parametrize("state", ["idle", "watching", "attention", "paused"])
def test_tray_icon_reads_at_sixteen_pixels(qt_app: QApplication, state: str) -> None:
    icon = icons.tray_icon(state, LIGHT_TOKENS)
    assert not icon.isNull()
    pixmap = icon.pixmap(16, 16)
    assert not pixmap.isNull()
    assert pixmap.size().width() == 16
    assert _distinct_colours(icon, 16) > 1, f"tray {state} rendered blank"


def test_tray_states_differ_from_each_other(qt_app: QApplication) -> None:
    rendered = {
        state: icons.tray_icon(state, LIGHT_TOKENS).pixmap(32, 32).toImage()
        for state in icons.tray_states()
    }
    assert len({image.constBits().tobytes() for image in rendered.values()}) == len(
        rendered
    )


def test_tray_icon_rejects_an_unknown_state(qt_app: QApplication) -> None:
    with pytest.raises(KeyError):
        icons.tray_icon("exploding")


def test_app_icon_is_multi_size(qt_app: QApplication) -> None:
    icon = icons.app_icon()
    assert not icon.isNull()
    widths = {size.width() for size in icon.availableSizes()}
    assert {16, 32, 256} <= widths
    assert _distinct_colours(icon, 32) > 1


def test_write_ico_produces_a_real_ico_file(qt_app: QApplication, tmp_path: Path) -> None:
    target = tmp_path / "nested" / "app.ico"
    icons.write_ico(target)

    assert target.exists()
    data = target.read_bytes()
    assert len(data) > 0
    assert data[:4] == b"\x00\x00\x01\x00"

    # The directory count must match the sizes we claim to ship, and every
    # entry must point inside the file.
    count = int.from_bytes(data[4:6], "little")
    assert count == len(icons.ICO_SIZES)
    for index in range(count):
        start = 6 + index * 16
        length = int.from_bytes(data[start + 8 : start + 12], "little")
        offset = int.from_bytes(data[start + 12 : start + 16], "little")
        assert length > 0
        assert offset + length <= len(data)
