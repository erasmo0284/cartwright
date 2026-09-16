r"""Icons, drawn at runtime from inline SVG.

Why not ship PNG or SVG files
-----------------------------

An icon that lives in ``assets/icons/*.svg`` is one PyInstaller ``--add-data``
line away from being missing in a release build, and a missing icon is a blank
square in the navigation list -- a visible defect with no error message. The
glyphs here are short enough to live in source, so there is nothing to bundle,
nothing to find at runtime and nothing to get out of sync with the theme.

Drawing them ourselves also solves recolouring. Qt cannot tint an arbitrary
``QIcon`` for dark mode; the usual workarounds are a second set of assets or a
``QPainter`` composition pass. Because the SVG is a string, the stroke colour
is substituted from the current token set before rasterising, so one glyph
definition serves both themes.

Every glyph is rasterised at 16, 20, 24 and 32 px and added to the
:class:`QIcon` as separate pixmaps. Qt would otherwise scale one bitmap, and
the 1.5px strokes turn to mush at 125% and 150% display scaling, which is
what most Windows 11 laptops ship with.

The drawing style is Fluent-ish on purpose: a 24x24 viewBox, 1.5px strokes,
round caps and joins, no fills, no detail that disappears below 16px.
"""

from __future__ import annotations

import struct
from pathlib import Path
from string import Template
from typing import ClassVar, Final, Mapping

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QImage, QImageWriter, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from app.ui.theme.tokens import DARK_TOKENS, LIGHT_TOKENS

#: Rasterisation sizes for interface icons. 16 and 20 cover 100% and 125%
#: scaling of a 16px icon; 24 and 32 cover 150% and the navigation list.
ICON_SIZES: Final[tuple[int, ...]] = (16, 20, 24, 32)

#: Sizes written into the ``.ico``. Windows picks 16 for the title bar, 32 for
#: the taskbar and Alt-Tab, 48 for Explorer's medium view and 256 for its
#: extra-large view; 20, 24 and 64 fill in the intermediate scale factors.
ICO_SIZES: Final[tuple[int, ...]] = (16, 20, 24, 32, 48, 64, 256)

_ICON_DOCUMENT: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'width="24" height="24" fill="none" stroke="${colour}" '
    'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
    "${body}</svg>"
)

#: The glyph bodies. A path of the form ``M12 8h.01`` is a zero-length segment
#: which, with a round cap, renders as a dot -- cheaper and crisper than a
#: filled circle, and it keeps every glyph to a single stroke colour.
_GLYPHS: Final[Mapping[str, str]] = {
    "dashboard": (
        '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/>'
        '<rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/>'
        '<rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/>'
        '<rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/>'
    ),
    "purchase": (
        '<path d="M5.5 8.5h13v10a2 2 0 0 1-2 2h-9a2 2 0 0 1-2-2z"/>'
        '<path d="M9 8.5V7a3 3 0 0 1 6 0v1.5"/>'
    ),
    "watchlist": (
        '<path d="M2.5 12S6 6.5 12 6.5 21.5 12 21.5 12 18 17.5 12 17.5 2.5 12 2.5 12z"/>'
        '<circle cx="12" cy="12" r="2.75"/>'
    ),
    "activity": '<path d="M3 12h4l2.5-6 4 12 2.5-6h5"/>',
    # Sliders rather than a gear: a gear's teeth vanish below 20px.
    "settings": (
        '<path d="M4 7h9"/><path d="M18 7h2"/><circle cx="15.5" cy="7" r="2.25"/>'
        '<path d="M4 17h4"/><path d="M13 17h7"/><circle cx="10.5" cy="17" r="2.25"/>'
    ),
    # A generic person. Deliberately no Amazon mark, wordmark or colour
    # anywhere in this application's iconography.
    "amazon_account": (
        '<circle cx="12" cy="8.5" r="3.75"/>'
        '<path d="M5 20c0-3.3 3.1-5.5 7-5.5s7 2.2 7 5.5"/>'
    ),
    "check": '<path d="M4.5 12.5l5 5 10-11"/>',
    "cross": '<path d="M6 6l12 12"/><path d="M18 6L6 18"/>',
    "warning": (
        '<path d="M12 4.5l8.5 15h-17z"/><path d="M12 10v4.4"/><path d="M12 17.4h.01"/>'
    ),
    "pause": '<path d="M9.5 5.5v13"/><path d="M14.5 5.5v13"/>',
    "play": '<path d="M8 5.5l11 6.5-11 6.5z"/>',
    "refresh": (
        '<path d="M4.5 12a7.5 7.5 0 0 1 12.8-5.3"/><path d="M17.5 3.5V7H14"/>'
        '<path d="M19.5 12a7.5 7.5 0 0 1-12.8 5.3"/><path d="M6.5 20.5V17H10"/>'
    ),
    "external": (
        '<path d="M14 5.5h5v5"/><path d="M19 5.5l-7.5 7.5"/>'
        '<path d="M17 14v3.5a2 2 0 0 1-2 2H6.5a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2H10"/>'
    ),
    "trash": (
        '<path d="M4.5 7.5h15"/><path d="M9.5 7.5V5.5h5v2"/>'
        '<path d="M6.5 7.5l.8 11.2a2 2 0 0 0 2 1.8h5.4a2 2 0 0 0 2-1.8l.8-11.2"/>'
    ),
    "edit": '<path d="M16.5 4.5l3 3L8 19H5v-3z"/>',
    "clock": '<circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3.5 2.5"/>',
    "cart": (
        '<path d="M3.5 5.5h2.2l2.3 9.5h9.5"/><path d="M6.6 8.5h13.9l-1.8 6.5"/>'
        '<circle cx="9.5" cy="19" r="1.5"/><circle cx="17" cy="19" r="1.5"/>'
    ),
    "shield": '<path d="M12 3.5l7.5 3v5.2c0 4.3-3 7.6-7.5 8.8-4.5-1.2-7.5-4.5-7.5-8.8V6.5z"/>',
    "bell": (
        '<path d="M18 16.5H6l1.4-2.3V11a4.6 4.6 0 0 1 9.2 0v3.2z"/>'
        '<path d="M10.2 19.5a2 2 0 0 0 3.6 0"/>'
    ),
    "info": '<circle cx="12" cy="12" r="8"/><path d="M12 11v5.5"/><path d="M12 8h.01"/>',
}

#: Tray states and the token whose colour marks them. The base shape never
#: changes -- only the dot -- so the icon stays recognisable as this
#: application while still saying what it is doing.
_TRAY_STATE_TOKENS: Final[Mapping[str, str]] = {
    "idle": "textMuted",
    "watching": "success",
    "attention": "dangerFill",
    "paused": "warning",
}

#: Tray glyph: a filled bag silhouette plus a state dot, ringed in the page
#: colour so the dot stays separate from the bag on any taskbar shade. Filled
#: rather than stroked because a 1.5px outline is illegible at 16px against
#: the taskbar's own texture.
_TRAY_DOCUMENT: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'width="24" height="24">'
    '<path d="M5 8h14v10.5a2.5 2.5 0 0 1-2.5 2.5h-9A2.5 2.5 0 0 1 5 18.5z" '
    'fill="${base}"/>'
    '<path d="M9 8V6.6a3 3 0 0 1 6 0V8" fill="none" stroke="${base}" '
    'stroke-width="1.8" stroke-linecap="round"/>'
    '<circle cx="18" cy="18" r="6" fill="${ring}"/>'
    '<circle cx="18" cy="18" r="4.4" fill="${dot}"/>'
    "</svg>"
)

#: The application mark: a shield with a check, on a solid accent tile. It
#: says "a purchase that was verified", which is what the product does, and it
#: shares no shape, mark or colour with Amazon's own branding.
#:
#: Fixed colours, not tokens: this icon appears on the taskbar, in Explorer,
#: in the installer and in the Action Center, none of which follow the
#: application's own light/dark choice.
_APP_TILE: Final[str] = LIGHT_TOKENS["accent"]
_APP_MARK: Final[str] = "#FFFFFF"

_APP_DOCUMENT: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" '
    'width="48" height="48">'
    f'<rect x="1" y="1" width="46" height="46" rx="10" fill="{_APP_TILE}"/>'
    f'<path d="M24 10l12 4.6v8.2c0 7.2-4.8 12.5-12 14.2-7.2-1.7-12-7-12-14.2v-8.2z" '
    f'fill="none" stroke="{_APP_MARK}" stroke-width="3" stroke-linejoin="round"/>'
    f'<path d="M18.5 23.8l4.3 4.3 7.4-8" fill="none" stroke="{_APP_MARK}" '
    'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
    "</svg>"
)


def _default_tokens() -> Mapping[str, str]:
    """Tokens matching the *system* appearance.

    Used for the tray icon, which sits on the taskbar and therefore follows
    Windows rather than the application's own theme choice. Falls back to the
    light set when there is no application object to ask.
    """
    app = QGuiApplication.instance()
    if app is None:
        return LIGHT_TOKENS
    hints = QGuiApplication.styleHints()
    if hints is not None and hints.colorScheme() == Qt.ColorScheme.Dark:
        return DARK_TOKENS
    return LIGHT_TOKENS


def _rasterise(svg: str, size: int) -> QPixmap:
    """Render an SVG string to a transparent square pixmap of ``size`` px.

    Painting into a :class:`QImage` rather than straight into a
    :class:`QPixmap` keeps this independent of the window system, so it works
    identically under the offscreen platform in the test suite.
    """
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter)
    painter.end()
    return QPixmap.fromImage(image)


def _multi_size_icon(svg: str, sizes: tuple[int, ...] = ICON_SIZES) -> QIcon:
    """A :class:`QIcon` holding one crisp pixmap per size in ``sizes``."""
    icon = QIcon()
    for size in sizes:
        icon.addPixmap(_rasterise(svg, size))
    return icon


class IconSet:
    """The interface icons, coloured for one token set.

    Create one per theme (``ThemeManager.theme_changed`` is the cue to make a
    new one) and hand it to the screens. Rendering is cached per name and
    colour, so asking twice costs nothing.
    """

    #: ``ClassVar`` is required: inside a class body a plain annotation on a
    #: dataclass becomes a field, and keeping the convention here means the
    #: two cannot drift.
    SIZES: ClassVar[tuple[int, ...]] = ICON_SIZES

    #: Every glyph name this set can produce.
    NAMES: ClassVar[tuple[str, ...]] = tuple(_GLYPHS)

    def __init__(self, tokens: Mapping[str, str] | None = None) -> None:
        # Falling back to the *current* scheme rather than to the light set
        # matters: a bare ``IconSet()`` in a dialog would otherwise render
        # near-black glyphs on a dark background. Callers that follow the
        # application's own theme (rather than the system's) should still pass
        # ``ThemeManager.current_tokens()`` explicitly and rebuild on
        # ``theme_changed``.
        self._tokens: Mapping[str, str] = dict(
            tokens if tokens is not None else _default_tokens()
        )
        self._cache: dict[tuple[str, str], QIcon] = {}

    @property
    def names(self) -> tuple[str, ...]:
        """The available glyph names."""
        return self.NAMES

    def svg(self, name: str, colour: str) -> str:
        """The SVG document for ``name`` stroked in ``colour``.

        Raises :class:`KeyError` for an unknown name, so a typo in a screen
        fails in that screen's test rather than showing an empty square.
        """
        try:
            body = _GLYPHS[name]
        except KeyError as exc:
            raise KeyError(f"No icon glyph named {name!r}") from exc
        return Template(_ICON_DOCUMENT).substitute(colour=colour, body=body)

    def icon(self, name: str, token: str = "text") -> QIcon:
        """The icon for ``name``, coloured from the token ``token``."""
        return self.coloured(name, self._tokens[token])

    def coloured(self, name: str, colour: str) -> QIcon:
        """The icon for ``name`` in an explicit hex ``colour``."""
        key = (name, colour)
        cached = self._cache.get(key)
        if cached is None:
            cached = _multi_size_icon(self.svg(name, colour))
            self._cache[key] = cached
        return cached

    def pixmap(self, name: str, size: int, token: str = "text") -> QPixmap:
        """A single pixmap, for places that cannot take a :class:`QIcon`."""
        return _rasterise(self.svg(name, self._tokens[token]), size)


def tray_icon(state: str, tokens: Mapping[str, str] | None = None) -> QIcon:
    """The notification-area icon for ``state``.

    ``state`` is one of ``idle``, ``watching``, ``attention`` or ``paused``;
    an unknown state raises rather than silently showing "idle", because a
    tray icon that under-reports "attention" hides the one thing the user
    needs to act on.
    """
    try:
        dot_token = _TRAY_STATE_TOKENS[state]
    except KeyError as exc:
        raise KeyError(
            f"Unknown tray state {state!r}; expected one of "
            f"{', '.join(sorted(_TRAY_STATE_TOKENS))}"
        ) from exc
    palette = dict(tokens or _default_tokens())
    svg = Template(_TRAY_DOCUMENT).substitute(
        base=palette["text"],
        ring=palette["bg"],
        dot=palette[dot_token],
    )
    return _multi_size_icon(svg)


def tray_states() -> tuple[str, ...]:
    """The accepted :func:`tray_icon` states."""
    return tuple(_TRAY_STATE_TOKENS)


def app_icon() -> QIcon:
    """The application icon, for window title bars and the task switcher."""
    return _multi_size_icon(_APP_DOCUMENT, ICO_SIZES)


def write_ico(path: Path) -> None:
    """Write the application icon as a multi-resolution ``.ico``.

    The installer, the shortcut and the Application User Model ID
    registration all need a real file on disk; Windows will not take a
    :class:`QIcon`.

    Route taken: Qt's ICO handler writes a *single* image per file, so each
    size is encoded to PNG with :class:`QImageWriter` and the ICO container
    (a 6-byte directory header plus one 16-byte entry per image) is assembled
    here with :mod:`struct`. PNG-compressed entries are valid ICO and are
    what Windows itself uses for the 256px size; every supported Windows
    version reads them at every size.
    """
    payloads = [(size, _png_bytes(_rasterise(_APP_DOCUMENT, size))) for size in ICO_SIZES]

    header = struct.pack("<HHH", 0, 1, len(payloads))
    # Image data starts after the header and the whole directory.
    offset = len(header) + 16 * len(payloads)
    directory = bytearray()
    for size, data in payloads:
        # 256 is stored as 0: the field is a single byte and 256 does not fit.
        dimension = 0 if size >= 256 else size
        directory += struct.pack(
            "<BBBBHHII",
            dimension,
            dimension,
            0,  # palette size; 0 for a true-colour image
            0,  # reserved
            1,  # colour planes
            32,  # bits per pixel
            len(data),
            offset,
        )
        offset += len(data)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + bytes(directory) + b"".join(data for _, data in payloads))


def _png_bytes(pixmap: QPixmap) -> bytes:
    """PNG encoding of ``pixmap``, via Qt's own writer."""
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QImageWriter(buffer, QByteArray(b"png"))
    if not writer.write(pixmap.toImage()):
        raise RuntimeError(f"Could not encode the application icon: {writer.errorString()}")
    buffer.close()
    return bytes(buffer.data())
