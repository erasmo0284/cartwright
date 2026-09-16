"""The reusable widget library every screen is assembled from.

Nothing in this package knows what the application does. A component takes a
value object -- a :class:`GuardReport`, a :class:`ProductSnapshot`, a
:class:`Money` -- and renders it; it never reads the database, never starts the
browser, and never decides whether a purchase may proceed. That keeps the
screens thin and makes each widget testable in isolation, which for the guard
table matters more than for anything else in the application.

Two conventions hold throughout, both enforced by
``tests/unit/test_components.py``:

* **No widget sets its own colours.** Every component carries an
  ``objectName`` or a ``role``/``severity``/``status`` dynamic property that
  :mod:`app.ui.theme.stylesheet` already addresses. A widget with a stylesheet
  of its own loses the native Windows 11 drawing and, worse, stops following
  the theme. Where a surface genuinely has to be painted (the sparkline, the
  busy overlay) it uses :class:`QPainter` with theme tokens.
* **Colour is never the only signal.** Every status carries words as well.

Dynamic properties written after construction go through
:func:`app.ui.components.common.repolish`, without which the change is stored
but never repainted.

The shell wires the theme in one line, at startup and on every change::

    theme.theme_changed.connect(
        lambda _appearance: apply_theme(window, theme.current_tokens())
    )

That is needed because a hand-painted widget cannot read the stylesheet, and
``QStyleHints.colorScheme()`` is not a reliable substitute -- see
:func:`app.ui.components.common.use_tokens`.
"""

from __future__ import annotations

from app.ui.components.badge import (
    StatusBadge,
    StatusDot,
    StatusLabel,
    severity_for_activity,
    severity_for_watch_status,
)
from app.ui.components.busy import BusyOverlay, ProgressStrip
from app.ui.components.buttons import (
    ButtonRow,
    DangerButton,
    IconButton,
    PrimaryButton,
    SubtleButton,
)
from app.ui.components.card import Card
from app.ui.components.common import (
    ElidingLabel,
    HLine,
    VLine,
    apply_theme,
    clear_layout,
    elide,
    icon_set,
    repolish,
    resolve_tokens,
    role_label,
    set_property,
    spacer,
    use_tokens,
)
from app.ui.components.empty_state import EmptyState
from app.ui.components.guard_table import GuardTable
from app.ui.components.metric import MetricTile
from app.ui.components.money_field import MoneyField, QuantityField
from app.ui.components.product_summary import ProductSummaryCard
from app.ui.components.sparkline import Sparkline

__all__ = [
    "BusyOverlay",
    "ButtonRow",
    "Card",
    "DangerButton",
    "ElidingLabel",
    "EmptyState",
    "GuardTable",
    "HLine",
    "IconButton",
    "MetricTile",
    "MoneyField",
    "PrimaryButton",
    "ProductSummaryCard",
    "ProgressStrip",
    "QuantityField",
    "Sparkline",
    "StatusBadge",
    "StatusDot",
    "StatusLabel",
    "SubtleButton",
    "VLine",
    "apply_theme",
    "clear_layout",
    "elide",
    "icon_set",
    "repolish",
    "resolve_tokens",
    "role_label",
    "set_property",
    "severity_for_activity",
    "severity_for_watch_status",
    "spacer",
    "use_tokens",
]
