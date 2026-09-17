r"""Render ``assets/social-preview.png``: the card GitHub shows when the
repository link is pasted into a chat, a post or a search result.

GitHub generates its own card when none is uploaded -- the owner's avatar, the
repository name and the description on a white background. That is fine and
says nothing. This draws the application's own icon and tagline instead, so a
shared link looks like a product rather than a directory listing.

1280x640 is GitHub's recommended size. Clients crop it in different ways and
some show it at thumbnail size in a chat list, so everything here is large,
high-contrast and kept well inside the edges: no text within 64px of a border,
and nothing smaller than 28px.

Run it after changing the icon or the tagline::

    .venv\Scripts\python.exe scripts\make_social_preview.py

The result is committed; it is not generated during a build, because GitHub
reads it from an upload in the repository settings rather than from the tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Deliberately *not* the offscreen platform, which is what the test suite
# uses: offscreen resolves no font families at all on Windows, so every
# glyph comes out as a box. This opens no window -- it only paints into a
# QImage -- but it needs the real platform plugin's font database, exactly
# as scripts/capture_screens.py does.

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.branding import BRAND  # noqa: E402
from app.ui.theme.fonts import ui_font  # noqa: E402

WIDTH, HEIGHT = 1280, 640
MARGIN = 84

#: Sampled from the icon itself rather than from the interface tokens: the
#: card has to sit behind the artwork without a seam, and the application's
#: dark background (#202020, a neutral grey) would read as a grey box around
#: a blue tile.
BACKGROUND_TOP = QColor("#0C1E3A")
BACKGROUND_BOTTOM = QColor("#050A14")
GLOW = QColor(31, 122, 255, 13)
TITLE = QColor("#FFFFFF")
TAGLINE = QColor("#C7DBF2")
FOOTNOTE = QColor("#7FA3C9")
RULE = QColor(92, 160, 230, 90)

ICON = PROJECT_ROOT / "assets" / "icons" / "app.png"
DESTINATION = PROJECT_ROOT / "assets" / "social-preview.png"

#: What the card says, under the name. Three short claims, because a chat
#: client may show this at a third of its size and a sentence would vanish.
POINTS = ("Deterministic purchase rules", "Test Mode by default", "Windows · MIT")


def _font(pixels: int, weight: QFont.Weight, role: str = "title") -> QFont:
    """The application's own type, sized in pixels.

    The card is a fixed-size image, so points -- which depend on the screen's
    DPI -- would change the layout depending on the machine that rendered it.
    :func:`ui_font` resolves the family exactly as the application does, and
    the point size it sets is overridden immediately.
    """
    font = ui_font(12, role=role, weight=weight)
    font.setPixelSize(pixels)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return font


def render() -> QImage:
    artwork = QImage(str(ICON))
    if artwork.isNull():
        raise FileNotFoundError(f"The application icon is missing: {ICON}")

    card = QImage(WIDTH, HEIGHT, QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(card)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

    gradient = QLinearGradient(0, 0, 0, HEIGHT)
    gradient.setColorAt(0.0, BACKGROUND_TOP)
    gradient.setColorAt(1.0, BACKGROUND_BOTTOM)
    painter.fillRect(card.rect(), gradient)

    # A soft pool of light behind the icon, so the tile does not float on a
    # flat field. Drawn as concentric ellipses because a radial gradient with
    # an alpha stop banded visibly at this size.
    icon_size = 336
    icon_x = MARGIN
    icon_y = (HEIGHT - icon_size) // 2
    centre_x = icon_x + icon_size / 2
    centre_y = icon_y + icon_size / 2
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(GLOW)
    for step in range(6, 0, -1):
        radius = icon_size * (0.50 + step * 0.032)
        painter.drawEllipse(QRectF(centre_x - radius, centre_y - radius, radius * 2, radius * 2))
    painter.setBrush(Qt.BrushStyle.NoBrush)

    painter.drawImage(
        QRectF(icon_x, icon_y, icon_size, icon_size),
        artwork,
        QRectF(0, 0, artwork.width(), artwork.height()),
    )

    text_x = icon_x + icon_size + 76
    text_width = WIDTH - text_x - MARGIN

    painter.setPen(TITLE)
    painter.setFont(_font(96, QFont.Weight.DemiBold))
    painter.drawText(
        QRectF(text_x, 176, text_width, 110),
        int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
        BRAND.display_name,
    )

    # Broken at the colon rather than left to word-wrap, which stranded
    # "buy." alone on the second line.
    head, _, tail = BRAND.tagline.partition(": ")
    painter.setPen(TAGLINE)
    painter.setFont(_font(35, QFont.Weight.Normal, role="body"))
    painter.drawText(
        QRectF(text_x, 286, text_width, 96),
        int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap),
        f"{head}:\n{tail}" if tail else BRAND.tagline,
    )

    painter.setPen(RULE)
    painter.drawLine(text_x, 400, text_x + 190, 400)

    painter.setPen(FOOTNOTE)
    painter.setFont(_font(30, QFont.Weight.Medium, role="body"))
    y = 432
    for point in POINTS:
        painter.drawText(
            QRectF(text_x, y, text_width, 44),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            point,
        )
        y += 44

    painter.end()
    return card


def main() -> int:
    app = QApplication.instance() or QApplication([])
    card = render()
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    if not card.save(str(DESTINATION), "PNG"):
        print(f"could not write {DESTINATION}", file=sys.stderr)
        return 1
    print(f"wrote {DESTINATION} ({DESTINATION.stat().st_size / 1024:.0f} KiB)")
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
