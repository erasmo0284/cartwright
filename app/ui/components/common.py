"""Primitives every widget in this package leans on.

Three Qt facts shape this module, and each one is a defect the first time you
meet it rather than something the API warns you about:

* A dynamic property that changes *after* the application stylesheet has been
  applied does not repaint. Qt resolves the selectors once, during polish, and
  nothing re-runs that resolution on a property write. :func:`repolish` is the
  documented workaround and every property write in this package goes through
  it (or through :func:`set_property`).
* A plain :class:`QWidget` ignores ``background-color`` and ``border`` from the
  application stylesheet unless it carries ``WA_StyledBackground``.
  :func:`enable_styled_background` is that one line, named so the reason is
  visible at each call site.
* :class:`QLabel` does not elide. It clips, or it grows its container. Amazon
  product titles are routinely 200 characters, so eliding is done here --
  :class:`ElidingLabel` for one line or a bounded number of wrapped lines.

No widget in this package configures its own colours; they set an
``objectName`` or a ``role`` property and let :mod:`app.ui.theme.stylesheet`
answer. :func:`role_label` pairs each ``role`` with the font that role was
designed for, so the two cannot drift apart across modules.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final, Mapping

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication, QResizeEvent, QTextLayout
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QWidget

from app.ui.theme import (
    DARK_TOKENS,
    LIGHT_TOKENS,
    IconSet,
    font_body,
    font_caption,
    font_metric,
    font_section,
    font_title,
)

_LOG: Final = logging.getLogger("app.ui.components.common")

#: ``role`` property value -> the font that role was designed for. Keeping the
#: pairing here means a caller cannot ask for the "metric" colour at caption
#: size, which is the usual way a type scale rots.
_ROLE_FONTS: Final[Mapping[str, Any]] = {
    "title": font_title,
    "subtitle": font_body,
    "caption": font_caption,
    "metric": font_metric,
    "metricLabel": font_caption,
    "sectionHeading": font_section,
}

#: Everything that is not a digit, separator or space in a formatted zero
#: amount: the currency's symbol, or its ISO code when it has no symbol.
_NON_NUMERIC: Final = re.compile(r"[\d.,\s]")


def repolish(widget: QWidget) -> None:
    """Re-resolve the application stylesheet for ``widget``.

    Required after **every** change to a dynamic property that a selector
    matches on (``severity``, ``status``, ``role``, ``invalid``). Qt evaluates
    those selectors when the widget is polished and caches the result; writing
    the property afterwards updates the value but repaints nothing, so a badge
    that turns from success to error keeps its old colour with no error
    anywhere to explain it.

    Unpolish-then-polish throws that cache away and resolves the selectors
    again. It is safe on a widget with no stylesheet in force -- the base style
    simply does nothing -- so callers never have to check first.
    """
    style = widget.style()
    if style is None:  # pragma: no cover - a widget always has a style
        return
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_property(widget: QWidget, name: str, value: Any) -> None:
    """Write dynamic property ``name`` and make the change visible."""
    widget.setProperty(name, value)
    repolish(widget)


def enable_styled_background(widget: QWidget) -> None:
    """Let the application stylesheet paint ``widget``'s surface.

    A direct :class:`QWidget` subclass draws nothing of its own, so a rule
    giving it a fill or a border is silently ignored. :class:`QFrame` and
    friends set this attribute themselves; a bare ``QWidget`` has to ask.
    """
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)


#: The token set this package draws with, once the shell has said which one is
#: in force. ``None`` means "nobody has told us", and Qt is asked instead.
_ACTIVE_TOKENS: Mapping[str, str] | None = None


def use_tokens(tokens: Mapping[str, str] | None) -> None:
    """Tell this package which token set the theme is currently using.

    Needed because a widget that paints itself cannot read the stylesheet, and
    asking Qt is not reliable: ``ThemeManager`` switches appearance through
    ``QStyleHints.setColorScheme``, and on platforms that do not act on that
    hint -- the offscreen plugin used by the tests among them --
    ``colorScheme()`` still answers ``Unknown``. A hand-painted sparkline
    would then draw the light-theme accent onto a dark card, which is a defect
    with no error attached to it.

    The application shell calls this once at startup and again on every
    ``ThemeManager.theme_changed``; :func:`apply_theme` does both in one step.
    ``None`` restores automatic detection.
    """
    global _ACTIVE_TOKENS
    _ACTIVE_TOKENS = tokens


def resolve_tokens(tokens: Mapping[str, str] | None = None) -> Mapping[str, str]:
    """The token set to draw with.

    An explicit argument wins, then whatever :func:`use_tokens` was last told,
    then Qt's own colour scheme, which is right on a platform that reports one
    and resolves to the light set when nothing does.
    """
    if tokens is not None:
        return tokens
    if _ACTIVE_TOKENS is not None:
        return _ACTIVE_TOKENS
    hints = QGuiApplication.styleHints()
    if hints is not None and hints.colorScheme() == Qt.ColorScheme.Dark:
        return DARK_TOKENS
    return LIGHT_TOKENS


def apply_theme(root: QWidget, tokens: Mapping[str, str] | None = None) -> None:
    """Re-read theme colours for every component under ``root``.

    Only the hand-painted surfaces and the rasterised icons need this: colours
    that come from the stylesheet are re-applied by Qt when the application
    stylesheet changes, but an icon was rasterised in the old text colour and
    a sparkline holds the old accent. One call after a theme change fixes
    both::

        theme.theme_changed.connect(
            lambda _appearance: apply_theme(window, theme.current_tokens())
        )
    """
    use_tokens(tokens)
    for child in root.findChildren(QWidget):
        hook = getattr(child, "refresh_theme", None)
        if callable(hook):
            hook()
    own_hook = getattr(root, "refresh_theme", None)
    if callable(own_hook):
        own_hook()


#: Icon sets by text colour. Rasterising a glyph is cheap but not free, and a
#: list of rows asks for the same icon once per row; two sets (light and dark)
#: cover the whole application, so they are held for the process lifetime.
_ICON_SETS: dict[str, IconSet] = {}


def icon_set(tokens: Mapping[str, str] | None = None) -> IconSet:
    """An :class:`IconSet` for ``tokens``, cached per appearance.

    Keyed on the token set's own text colour, so a custom palette gets its own
    set instead of quietly reusing the light one.
    """
    palette = resolve_tokens(tokens)
    key = palette["text"]
    cached = _ICON_SETS.get(key)
    if cached is None:
        cached = IconSet(palette)
        _ICON_SETS[key] = cached
    return cached


def role_label(
    text: str = "", role: str | None = None, parent: QWidget | None = None
) -> QLabel:
    """A :class:`QLabel` carrying a stylesheet ``role`` and that role's font.

    ``role`` of ``None`` means body copy: no property, no font override, so
    the label inherits the application font and the palette's text colour.
    """
    label = QLabel(text, parent)
    if role is not None:
        label.setProperty("role", role)
        factory = _ROLE_FONTS.get(role)
        if factory is not None:
            label.setFont(factory())
        else:  # An unrecognised role is a typo; the colour will not apply.
            _LOG.warning("Unknown label role %r; no font applied", role)
    return label


def elide(label: QLabel, text: str, max_lines: int = 1) -> str:
    """Set ``text`` on ``label``, shortened to fit ``max_lines`` of its width.

    Returns the text actually shown. The full text becomes the tooltip when
    anything was dropped, so nothing is unreachable.
    """
    shown = elided_text(text, label, max_lines)
    label.setText(shown)
    label.setToolTip(text if shown != text else "")
    return shown


def elided_text(text: str, label: QLabel, max_lines: int = 1) -> str:
    """``text`` shortened with an ellipsis to fit ``max_lines`` of ``label``.

    One line is Qt's own :meth:`QFontMetrics.elidedText`. More than one needs
    :class:`QTextLayout`, because the question "where does line 3 begin?" can
    only be answered by breaking the text exactly as the label will: the lines
    are laid out at the label's width, everything from the start of the line
    after the budget is dropped, and the final kept line is elided.
    """
    width = label.contentsRect().width()
    metrics = label.fontMetrics()
    if width <= 0 or not text:
        return text
    if max_lines <= 1:
        return metrics.elidedText(text, Qt.TextElideMode.ElideRight, width)

    layout = QTextLayout(text, label.font())
    layout.beginLayout()
    starts: list[int] = []
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        starts.append(line.textStart())
    layout.endLayout()

    if len(starts) <= max_lines:
        return text
    keep_from = starts[max_lines - 1]
    head, tail = text[:keep_from], text[keep_from:]
    return head + metrics.elidedText(tail, Qt.TextElideMode.ElideRight, width)


class ElidingLabel(QLabel):
    """A label that shortens its text to the width it was actually given.

    Qt sizes a label to its text and then clips whatever does not fit, which
    for a product title means either a horizontally scrolling row or a
    truncation with no ellipsis to show that it happened. This re-elides on
    every resize instead, and keeps the untruncated string in
    :attr:`full_text` (and in the tooltip) so the user can still read it.
    """

    def __init__(
        self,
        text: str = "",
        role: str | None = None,
        max_lines: int = 1,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._full_text = text
        self._max_lines = max(1, max_lines)
        if role is not None:
            self.setProperty("role", role)
            factory = _ROLE_FONTS.get(role)
            if factory is not None:
                self.setFont(factory())
        if self._max_lines > 1:
            self.setWordWrap(True)
        # Without this the label reports its full text width as a minimum and
        # the row it sits in simply grows instead of eliding.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._apply()

    @property
    def full_text(self) -> str:
        """The text as it was set, before any shortening."""
        return self._full_text

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        """Remember ``text`` in full, then show as much of it as fits."""
        self._full_text = text
        self._apply()

    def set_max_lines(self, max_lines: int) -> None:
        """Change how many wrapped lines the text may occupy."""
        self._max_lines = max(1, max_lines)
        self.setWordWrap(self._max_lines > 1)
        self._apply()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._apply()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        """Enough room for a few characters and the ellipsis.

        An ignored horizontal size policy is what makes eliding possible, but
        it also lets a layout squeeze the label to nothing -- and a value that
        vanishes entirely is worse than one that is truncated, because there
        is nothing left to hint that it was ever there.
        """
        metrics = self.fontMetrics()
        return QSize(
            metrics.horizontalAdvance("00000…"),
            metrics.height() * self._max_lines,
        )

    def _apply(self) -> None:
        shown = elided_text(self._full_text, self, self._max_lines)
        # Only write when it differs: setText inside resizeEvent can otherwise
        # bounce the layout between two widths forever.
        if shown != super().text():
            super().setText(shown)
        self.setToolTip(self._full_text if shown != self._full_text else "")


def HLine(parent: QWidget | None = None) -> QFrame:  # noqa: N802 - reads as a widget
    """A one-pixel horizontal rule, drawn by the stylesheet's ``#Separator``."""
    line = QFrame(parent)
    line.setObjectName("Separator")
    # NoFrame, not HLine: the shape would have QFrame draw a second, native
    # rule on top of the one the stylesheet fills.
    line.setFrameShape(QFrame.Shape.NoFrame)
    line.setFixedHeight(1)
    line.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return line


def VLine(parent: QWidget | None = None) -> QFrame:  # noqa: N802 - reads as a widget
    """A one-pixel vertical rule.

    Uses ``#SeparatorVertical`` rather than ``#Separator``: the horizontal rule
    is pinned to a one-pixel *height*, which for a vertical divider would make
    it a dot. The stylesheet already carries both names.
    """
    line = QFrame(parent)
    line.setObjectName("SeparatorVertical")
    line.setFrameShape(QFrame.Shape.NoFrame)
    line.setFixedWidth(1)
    line.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
    return line


def clear_layout(layout: Any) -> None:
    """Remove and destroy every widget in ``layout``.

    ``takeAt`` detaches a widget from the *layout* but leaves it parented to
    the containing widget, and ``deleteLater`` only schedules destruction for
    the next event-loop pass. A widget in that state keeps painting at
    whatever geometry it last had, so a list that is rebuilt twice before the
    event loop runs renders its old rows on top of its new ones.

    Reparenting to ``None`` first detaches it from the paint tree
    immediately, which is what actually stops the ghost rows.
    """
    while layout.count():
        item = layout.takeAt(0)
        if item is None:
            continue
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
            continue
        child = item.layout()
        if child is not None:
            clear_layout(child)


def spacer(width: int = 0, height: int = 0) -> QWidget:
    """A blank widget of the given size.

    A dimension of ``0`` expands to fill instead, so ``spacer()`` is a stretch,
    ``spacer(height=8)`` is a fixed vertical gap, and neither needs a layout
    item type the caller has to remember.
    """
    widget = QWidget()
    horizontal = QSizePolicy.Policy.Fixed if width else QSizePolicy.Policy.Expanding
    vertical = QSizePolicy.Policy.Fixed if height else QSizePolicy.Policy.Expanding
    widget.setSizePolicy(horizontal, vertical)
    if width:
        widget.setFixedWidth(width)
    if height:
        widget.setFixedHeight(height)
    return widget


def currency_prefix(currency: str) -> str:
    """The symbol to show in front of an amount field, e.g. ``$`` or ``SEK``.

    Derived from :meth:`Money.format` rather than from a second symbol table:
    one table that can disagree with the formatter is enough.
    """
    from app.core.money import Money

    return _NON_NUMERIC.sub("", Money.zero(currency).format()) or currency
