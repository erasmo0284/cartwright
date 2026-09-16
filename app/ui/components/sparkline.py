"""A price history line, drawn by hand.

QtCharts is not in PySide6-Essentials and is not worth adding for this: the
widget is one polyline and an optional dashed rule, and a charting dependency
would bring a second theming system, a second set of fonts and an installer
that is several megabytes larger.

The interesting part is the degenerate cases, because a price series routinely
hits all of them. No observations yet (draw nothing), one observation (a dot --
a line needs two points), and a price that has not moved at all, which is the
common case and the one where ``(value - low) / (high - low)`` divides by
zero. Each is handled explicitly rather than guarded by a single epsilon,
because "the price never changed" deserves to look different from "the price
went flat here".

Only ``Money.cents`` is read, never compared as :class:`Money`, so a series
that somehow mixes currencies draws something meaningless rather than raising
inside a paint event, where an exception would take the window with it.
"""

from __future__ import annotations

from typing import Final, Mapping, Sequence

from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from app.core.money import Money
from app.ui.components.common import resolve_tokens

#: Overall height. Tall enough for a shape to be legible, short enough to sit
#: inside a list row without changing its rhythm.
HEIGHT: Final[int] = 40

#: Preferred width. The widget expands, so this only matters in a layout that
#: has spare room to give away.
PREFERRED_WIDTH: Final[int] = 160

#: Inset, so the stroke and the end dot are not clipped by the widget edge.
_PADDING: Final[float] = 3.0

_LINE_WIDTH: Final[float] = 1.5
_DOT_RADIUS: Final[float] = 2.0


class Sparkline(QWidget):
    """A tiny price-history chart with an optional target line.

    Not interactive, and not a substitute for the numbers: the tooltip and the
    accessible description carry the low, high and latest values as text, so
    the shape is a summary rather than the only way to read the data.
    """

    def __init__(
        self,
        tokens: Mapping[str, str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._tokens: Mapping[str, str] = resolve_tokens(tokens)
        self._series: tuple[int, ...] = ()
        self._currency: str = ""
        self._target: int | None = None

        self.setFixedHeight(HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Price history")
        self._refresh_description()

    # -- public API ---------------------------------------------------------

    def set_series(self, points: Sequence[Money]) -> None:
        """Replace the plotted series, oldest observation first."""
        self._series = tuple(point.cents for point in points)
        self._currency = points[-1].currency if points else ""
        self._refresh_description()
        self.update()

    def set_target(self, target: Money | None) -> None:
        """Draw (or remove) the dashed line marking the user's target price."""
        self._target = None if target is None else target.cents
        self._refresh_description()
        self.update()

    def set_tokens(self, tokens: Mapping[str, str] | None) -> None:
        """Recolour with an explicit token set."""
        self._tokens = resolve_tokens(tokens)
        self.update()

    def refresh_theme(self) -> None:
        """Pick up the token set now in force.

        Called by :func:`app.ui.components.common.apply_theme`; the accent
        this widget painted with belongs to the theme it was built under.
        """
        self._tokens = resolve_tokens(None)
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(PREFERRED_WIDTH, HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        # Narrow enough to be squeezed into a dense row, wide enough that the
        # single-point dot still has room to be drawn.
        return QSize(16, HEIGHT)

    @property
    def point_count(self) -> int:
        """How many observations are plotted."""
        return len(self._series)

    # -- painting -----------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt naming
        """Draw the series, then the target line over it."""
        if not self._series:
            # No observations is not an error state and gets no placeholder
            # graphic: the row's own text already says "never checked".
            return

        width = self.width() - 2 * _PADDING
        height = self.height() - 2 * _PADDING
        if width <= 0 or height <= 0:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        try:
            low, high = self._bounds()
            span = high - low
            accent = QColor(self._tokens["accent"])

            if self._target is not None:
                pen = QPen(QColor(self._tokens["textMuted"]))
                pen.setStyle(Qt.PenStyle.DashLine)
                pen.setWidthF(1.0)
                painter.setPen(pen)
                y = self._y(self._target, low, span, height)
                painter.drawLine(QPointF(_PADDING, y), QPointF(_PADDING + width, y))

            pen = QPen(accent)
            pen.setWidthF(_LINE_WIDTH)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)

            if len(self._series) == 1:
                # One point cannot be a line. A dot in the middle says
                # "observed once" rather than implying a trend.
                painter.setBrush(accent)
                painter.drawEllipse(
                    QPointF(_PADDING + width / 2, _PADDING + height / 2),
                    _DOT_RADIUS,
                    _DOT_RADIUS,
                )
                return

            step = width / (len(self._series) - 1)
            polygon = QPolygonF(
                [
                    QPointF(
                        _PADDING + index * step,
                        self._y(value, low, span, height),
                    )
                    for index, value in enumerate(self._series)
                ]
            )
            painter.drawPolyline(polygon)
        finally:
            painter.end()

    # -- internals ----------------------------------------------------------

    def _bounds(self) -> tuple[int, int]:
        """``(low, high)`` across the series and the target.

        The target is included so that a target far below the observed prices
        is still visible instead of being clamped to the bottom edge, which
        would make an unreachable target look almost met.
        """
        values = list(self._series)
        if self._target is not None:
            values.append(self._target)
        return min(values), max(values)

    def _y(self, value: int, low: int, span: int, height: float) -> float:
        """Vertical position for ``value``; centred when the series is flat."""
        if span <= 0:
            return _PADDING + height / 2
        # Inverted: a higher price belongs nearer the top.
        return _PADDING + height * (1.0 - (value - low) / span)

    def _refresh_description(self) -> None:
        """Put the numbers in the tooltip, so the shape is never the only clue."""
        if not self._series:
            self.setToolTip("No price history yet")
            self.setAccessibleDescription("No price history yet")
            return
        currency = self._currency or "USD"
        low = Money(min(self._series), currency).format()
        high = Money(max(self._series), currency).format()
        latest = Money(self._series[-1], currency).format()
        parts = [f"Latest {latest}", f"Low {low}", f"High {high}"]
        if self._target is not None:
            parts.append(f"Target {Money(self._target, currency).format()}")
        text = " · ".join(parts)
        self.setToolTip(text)
        self.setAccessibleDescription(text)
