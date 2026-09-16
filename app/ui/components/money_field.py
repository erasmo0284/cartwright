"""Amount and quantity inputs.

A price limit is the one number in this application that decides whether money
moves, so the field that captures it is written to be forgiving about *form*
and unforgiving about *value*. It accepts ``109.97``, ``$109.97`` and
``1,299.00`` -- all of which a person legitimately types or pastes off the
product page -- and it refuses a negative or absurd amount outright.

Parsing is :mod:`app.core.money`'s job, not this widget's. That module resolves
every separator ambiguity in the direction that *blocks* a purchase, and it is
the module the guard's own tests cover. A second parser here would be a second
place for ``1.299`` to be read as one pound thirty.

Problems are reported as a caption under the field, never as a dialog: a modal
"invalid input" box on every keystroke makes a field unusable, and the user is
still mid-thought.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Final

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.core.money import DEFAULT_CURRENCY, Money, parse_money, parse_money_ceiling
from app.ui.components.common import currency_prefix, role_label, set_property

_LOG: Final = logging.getLogger("app.ui.components.money_field")

#: Upper bound, in major units. Nothing this application is for costs six
#: figures, and a mistyped extra digit is far more likely than a genuine
#: $1,000,000 limit -- so the typo is refused rather than acted on.
MAX_MAJOR_UNITS: Final[Decimal] = Decimal("100000")

#: Quantity bounds. Amazon itself caps most line items around 30, and a
#: runaway quantity is a spending mistake, not a convenience.
MIN_QUANTITY: Final[int] = 1
MAX_QUANTITY: Final[int] = 30


class MoneyField(QWidget):
    """A money input that reports its own problems in place.

    ``value()`` is ``None`` for a blank field, which means "no limit", and is
    also ``None`` while the text is unreadable -- :meth:`is_valid`
    distinguishes the two, and callers that are about to spend money must ask.
    """

    #: Carries a :class:`Money` or ``None``. Typed as ``object`` because Qt
    #: would otherwise coerce ``None`` into a default-constructed value.
    value_changed = Signal(object)

    def __init__(
        self,
        currency: str = DEFAULT_CURRENCY,
        placeholder: str = "No limit",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._currency = currency
        self._value: Money | None = None
        self._valid = True

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self._prefix = role_label(currency_prefix(currency), "caption", self)
        # The input keeps its default object name on purpose: the stylesheet
        # leaves QLineEdit alone, so this stays a native Windows 11 field with
        # its own hover and focus animations.
        self._input = QLineEdit(self)
        self._input.setPlaceholderText(placeholder)
        self._input.setAccessibleName("Maximum price")
        self._input.setClearButtonEnabled(True)
        self._input.textChanged.connect(self._on_text_changed)
        row.addWidget(self._prefix)
        row.addWidget(self._input, 1)
        column.addLayout(row)

        self._problem = role_label("", "caption", self)
        self._problem.setWordWrap(True)
        self._problem.setVisible(False)
        column.addWidget(self._problem)

        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    # -- public API ---------------------------------------------------------

    def value(self) -> Money | None:
        """The amount entered, or ``None`` for blank or unreadable text."""
        return self._value

    def set_value(self, value: Money | None) -> None:
        """Put ``value`` in the field, or clear it.

        Written without its currency symbol -- the prefix label already shows
        one, and two would look like a fault -- but with thousands separators,
        because ``1,299.00`` is how the amount is read back.
        """
        if value is None:
            self._input.setText("")
            return
        self._currency = value.currency
        self._prefix.setText(currency_prefix(value.currency))
        self._input.setText(f"{value.amount:,}")

    def is_valid(self) -> bool:
        """Whether the current text is usable. A blank field is valid."""
        return self._valid

    def text(self) -> str:
        """The raw text, for callers that want to persist it verbatim."""
        return self._input.text()

    @property
    def line_edit(self) -> QLineEdit:
        """The underlying field, for focus handling and tab order."""
        return self._input

    @property
    def problem(self) -> str:
        """The problem currently shown, or an empty string."""
        return self._problem.text()

    # -- internals ----------------------------------------------------------

    def _on_text_changed(self, _text: str) -> None:
        value, problem = self._interpret(self._input.text())
        self._value = value
        self._valid = problem is None
        self._problem.setText(problem or "")
        self._problem.setVisible(problem is not None)
        # The native field is left unstyled, but ``invalid`` is the property
        # the stylesheet's own paste field uses, so the two agree if this
        # field is ever promoted to a drawn one.
        set_property(self._input, "invalid", not self._valid)
        self.value_changed.emit(value)

    def _interpret(self, raw: str) -> tuple[Money | None, str | None]:
        """``(value, problem)`` for ``raw``. A problem of ``None`` means valid.

        The order of the checks matters. The sign is tested on the text before
        parsing, because :func:`parse_money` reads price *text* off a product
        page, where a leading minus is punctuation rather than a negative
        amount -- so ``-5`` would otherwise come back as five dollars.
        """
        text = raw.strip()
        if not text:
            return None, None
        if "-" in text or "(" in text:
            return None, "Enter an amount greater than zero."

        # Strict first; the ceiling parser then covers pasted price text that
        # repeats itself ("$109.97$109.97"), which Amazon's markup does.
        money = parse_money(text, default_currency=self._currency)
        if money is None:
            money = parse_money_ceiling(text, default_currency=self._currency)
        if money is None:
            return None, "Enter an amount such as 109.97."

        if money.cents < 0:
            return None, "Enter an amount greater than zero."
        if money.amount > MAX_MAJOR_UNITS:
            cap = Money.from_decimal(MAX_MAJOR_UNITS, money.currency)
            return None, f"Enter an amount below {cap.format()}."
        return money, None


class QuantityField(QWidget):
    """A whole-number quantity, with what is actually available beside it."""

    #: Emitted with the new quantity whenever it changes.
    value_changed = Signal(int)

    def __init__(self, value: int = MIN_QUANTITY, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._available: int | None = None

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        # Native QSpinBox: the stylesheet does not touch it, so it keeps the
        # Windows 11 arrows rather than two hand-drawn triangles.
        self._spin = QSpinBox(self)
        self._spin.setRange(MIN_QUANTITY, MAX_QUANTITY)
        self._spin.setValue(max(MIN_QUANTITY, min(MAX_QUANTITY, value)))
        self._spin.setAccessibleName("Quantity")
        self._spin.valueChanged.connect(self.value_changed.emit)
        row.addWidget(self._spin)

        self._hint = role_label("", "caption", self)
        self._hint.setVisible(False)
        row.addWidget(self._hint, 1)

        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def value(self) -> int:
        """The quantity currently selected."""
        return self._spin.value()

    def set_value(self, value: int) -> None:
        """Set the quantity, clamped to what is selectable."""
        self._spin.setValue(value)

    def set_maximum_available(self, available: int | None) -> None:
        """Limit the quantity to ``available``, and say so.

        Clamping silently would leave the user believing they had asked for
        four of something when the order will contain two, so the hint spells
        out the number the limit came from. ``None`` restores the default
        ceiling and hides the hint.
        """
        self._available = available
        if available is None:
            self._spin.setMaximum(MAX_QUANTITY)
            self._hint.setVisible(False)
            return
        ceiling = max(MIN_QUANTITY, min(MAX_QUANTITY, available))
        if available < MIN_QUANTITY:
            _LOG.debug("Reported availability of %s clamped to %s", available, ceiling)
        # setMaximum pulls the current value down with it, which is the whole
        # point: the field can never hold an unbuyable quantity.
        self._spin.setMaximum(ceiling)
        self._hint.setText(f"{ceiling} available")
        self._hint.setVisible(True)

    @property
    def maximum_available(self) -> int | None:
        """The availability last reported, unclamped."""
        return self._available

    @property
    def spin_box(self) -> QSpinBox:
        """The underlying control, for focus handling and tab order."""
        return self._spin
