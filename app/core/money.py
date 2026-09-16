"""Money values and price parsing.

Money is represented as an integer number of minor units (cents) so that no
float rounding can ever creep into a purchase decision.

Parsing scraped price text is safety-critical: a price read *too low* could
let the program buy something more expensive than the user allowed, while a
price read *too high* merely blocks the purchase. Every ambiguity in this
module is therefore resolved in the direction that blocks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

DEFAULT_CURRENCY: Final = "USD"

#: Currency symbol -> ISO 4217 code, covering the Amazon marketplaces.
_SYMBOL_TO_CURRENCY: Final[dict[str, str]] = {
    "$": "USD",
    "US$": "USD",
    "£": "GBP",
    "€": "EUR",
    "¥": "JPY",
    "₹": "INR",
    "C$": "CAD",
    "CA$": "CAD",
    "A$": "AUD",
    "R$": "BRL",
    "MX$": "MXN",
    "zł": "PLN",
    "kr": "SEK",
}

#: Currencies with no minor unit: amounts are whole numbers.
_ZERO_DECIMAL_CURRENCIES: Final[frozenset[str]] = frozenset({"JPY", "KRW"})

_CURRENCY_TO_SYMBOL: Final[dict[str, str]] = {
    "USD": "$",
    "GBP": "£",
    "EUR": "€",
    "JPY": "¥",
    "INR": "₹",
    "CAD": "CA$",
    "AUD": "A$",
    "BRL": "R$",
    "MXN": "MX$",
    "PLN": "zł",
    "SEK": "kr",
}


class MoneyError(ValueError):
    """Raised for malformed money input or cross-currency arithmetic."""


def minor_units(currency: str) -> int:
    """Number of decimal places a currency uses."""
    return 0 if currency.upper() in _ZERO_DECIMAL_CURRENCIES else 2


@dataclass(frozen=True, order=False)
class Money:
    """An exact monetary amount.

    ``cents`` is the amount in minor units. For zero-decimal currencies such
    as JPY the "minor unit" is the whole unit, matching ISO 4217.
    """

    cents: int
    currency: str = DEFAULT_CURRENCY

    def __post_init__(self) -> None:
        if isinstance(self.cents, bool) or not isinstance(self.cents, int):
            raise MoneyError(f"Money.cents must be an int, got {self.cents!r}")
        if not self.currency or len(self.currency) != 3:
            raise MoneyError(f"Invalid currency code: {self.currency!r}")

    # ---- constructors ---------------------------------------------------

    @classmethod
    def zero(cls, currency: str = DEFAULT_CURRENCY) -> Money:
        return cls(0, currency)

    @classmethod
    def from_decimal(
        cls, value: Decimal | int | str, currency: str = DEFAULT_CURRENCY
    ) -> Money:
        """Build from a major-unit amount, e.g. ``Decimal("109.97")``."""
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ArithmeticError) as exc:
            raise MoneyError(f"Not a valid amount: {value!r}") from exc
        if not amount.is_finite():
            raise MoneyError(f"Not a finite amount: {value!r}")
        scale = 10 ** minor_units(currency)
        quantised = (amount * scale).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return cls(int(quantised), currency)

    # ---- accessors ------------------------------------------------------

    @property
    def amount(self) -> Decimal:
        """The value in major units, exact."""
        places = minor_units(self.currency)
        scale = 10**places
        return (Decimal(self.cents) / Decimal(scale)).quantize(
            Decimal(1).scaleb(-places)
        )

    def is_zero(self) -> bool:
        return self.cents == 0

    # ---- arithmetic -----------------------------------------------------

    def _assert_same_currency(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise MoneyError(f"Expected Money, got {other!r}")
        if self.currency != other.currency:
            raise MoneyError(
                f"Cannot combine {self.currency} with {other.currency}; "
                "currency conversion is deliberately unsupported"
            )

    def __add__(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(self.cents + other.cents, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(self.cents - other.cents, self.currency)

    def __mul__(self, factor: int) -> Money:
        if isinstance(factor, bool) or not isinstance(factor, int):
            raise MoneyError("Money may only be multiplied by a whole quantity")
        return Money(self.cents * factor, self.currency)

    __rmul__ = __mul__

    def __neg__(self) -> Money:
        return Money(-self.cents, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.cents < other.cents

    def __le__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.cents <= other.cents

    def __gt__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.cents > other.cents

    def __ge__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.cents >= other.cents

    # ---- presentation ---------------------------------------------------

    def format(self, *, with_currency_code: bool = False) -> str:
        """Human-readable amount, e.g. ``$1,299.00``."""
        digits = minor_units(self.currency)
        symbol = _CURRENCY_TO_SYMBOL.get(self.currency, "")
        sign = "-" if self.cents < 0 else ""
        magnitude = abs(self.amount)
        body = f"{magnitude:,.{digits}f}"
        if symbol:
            text = f"{sign}{symbol}{body}"
        else:
            text = f"{sign}{body} {self.currency}"
        if with_currency_code and symbol:
            text = f"{text} {self.currency}"
        return text

    def __str__(self) -> str:
        return self.format()

    def __repr__(self) -> str:
        return f"Money({self.cents}, {self.currency!r})"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

#: A currency symbol optionally attached to a number, or a bare number.
_PRICE_TOKEN = re.compile(
    r"(?P<symbol>US\$|CA\$|MX\$|A\$|R\$|C\$|[$£€¥₹]|zł)?"
    r"\s*"
    r"(?P<number>"
    r"\d{1,3}(?:[., \s]\d{3})+(?:[.,]\d{1,2})?"
    r"|"
    r"\d+(?:[.,]\d{1,2})?"
    r")"
)

#: Locales that write the symbol after the number (``1.299,00 EUR``).
_TRAILING_SYMBOL = re.compile(
    r"(?P<number>\d{1,3}(?:[., \s]\d{3})*(?:[.,]\d{1,2})?)"
    r"\s*"
    r"(?P<symbol>US\$|[$£€¥]|zł|kr)"
)

#: Text that means "there is no usable price here".
_NO_PRICE_MARKERS: Final[tuple[str, ...]] = (
    "unavailable",
    "see all buying options",
    "no featured offers",
    "out of stock",
    "price not available",
)


def _normalise_number(raw: str) -> Decimal | None:
    """Turn a localised number string into a :class:`Decimal`.

    Separator ambiguity is resolved as follows:

    * both ``.`` and ``,`` present -> the *last* one is the decimal separator;
    * a single separator followed by exactly three digits is a thousands
      separator (``1,299`` -> 1299); this also rounds ``1.299`` up to 1299
      rather than down to 1.30, which is the blocking direction;
    * otherwise a single separator is the decimal separator.
    """
    text = raw.replace(" ", "").replace(" ", "")
    if not text:
        return None

    last_comma = text.rfind(",")
    last_period = text.rfind(".")

    if last_comma >= 0 and last_period >= 0:
        if last_comma > last_period:
            integer, _, fraction = text.rpartition(",")
        else:
            integer, _, fraction = text.rpartition(".")
        integer = integer.replace(".", "").replace(",", "")
    elif last_comma >= 0 or last_period >= 0:
        sep = "," if last_comma >= 0 else "."
        head, _, tail = text.rpartition(sep)
        groups = text.split(sep)
        rest_are_triples = all(len(group) == 3 for group in groups[1:])
        if len(tail) == 3 and rest_are_triples:
            # Thousands separator only: 1,299 or 1.299.000
            integer, fraction = text.replace(sep, ""), ""
        else:
            integer, fraction = head.replace(",", "").replace(".", ""), tail
    else:
        integer, fraction = text, ""

    if not integer:
        integer = "0"
    if not integer.isdigit() or (fraction and not fraction.isdigit()):
        return None
    try:
        return Decimal(f"{integer}.{fraction or '0'}")
    except InvalidOperation:
        return None


def extract_prices(
    text: str | None, *, default_currency: str = DEFAULT_CURRENCY
) -> list[Money]:
    """Every distinct price found in ``text``, in order of appearance.

    Duplicates are collapsed, which matters because Amazon's price markup
    routinely repeats the same value in a screen-reader span and a visible
    span (``$109.97$109.97``).
    """
    if not text:
        return []

    found: list[Money] = []
    seen: set[tuple[int, str]] = set()

    def record(symbol: str | None, number: str) -> None:
        mapped = _SYMBOL_TO_CURRENCY.get((symbol or "").strip())
        resolved = mapped or default_currency
        value = _normalise_number(number)
        if value is None:
            return
        try:
            money = Money.from_decimal(value, resolved)
        except MoneyError:
            return
        key = (money.cents, money.currency)
        if key not in seen:
            seen.add(key)
            found.append(money)

    consumed: list[tuple[int, int]] = []
    for match in _TRAILING_SYMBOL.finditer(text):
        record(match.group("symbol"), match.group("number"))
        consumed.append(match.span())

    for match in _PRICE_TOKEN.finditer(text):
        start, end = match.span()
        if any(start < c_end and end > c_start for c_start, c_end in consumed):
            continue
        record(match.group("symbol"), match.group("number"))

    return found


def parse_money(
    text: str | None, *, default_currency: str = DEFAULT_CURRENCY
) -> Money | None:
    """Strictly parse text that should contain exactly one price.

    Returns ``None`` when there is no price, when the text signals that no
    price exists, or when several *different* prices are present. Callers that
    knowingly read noisy text should use :func:`parse_money_ceiling`.
    """
    if not text:
        return None
    lowered = text.strip().lower()
    if any(marker in lowered for marker in _NO_PRICE_MARKERS):
        return None
    prices = extract_prices(text, default_currency=default_currency)
    if len(prices) != 1:
        return None
    return prices[0]


def parse_money_ceiling(
    text: str | None, *, default_currency: str = DEFAULT_CURRENCY
) -> Money | None:
    """Parse noisy text, returning the **largest** price found.

    Used where the source text may legitimately contain more than one number
    (a per-unit price, a struck-through list price). Choosing the maximum
    means a mis-read can only ever make the program refuse to buy, never
    overspend. Returns ``None`` if the text mixes currencies.
    """
    prices = extract_prices(text, default_currency=default_currency)
    if not prices:
        return None
    if len({price.currency for price in prices}) > 1:
        return None
    return max(prices, key=lambda money: money.cents)


def parse_split_price(
    whole: str | None,
    fraction: str | None,
    *,
    currency: str = DEFAULT_CURRENCY,
    symbol: str | None = None,
) -> Money | None:
    """Rebuild a price from Amazon's split ``a-price-whole``/``a-price-fraction``.

    The whole part arrives with a trailing decimal separator baked in
    (``"109."``) and may carry thousands separators (``"1,299."``). Naive
    concatenation of the two text nodes yields ``10997`` -- a hundredfold
    error -- so this case is handled explicitly rather than by the general
    parser.
    """
    if whole is None:
        return None
    resolved = _SYMBOL_TO_CURRENCY.get((symbol or "").strip(), currency)
    whole_digits = re.sub(r"\D", "", whole)
    if not whole_digits:
        return None
    fraction_digits = re.sub(r"\D", "", fraction or "")
    places = minor_units(resolved)
    if places == 0:
        return Money(int(whole_digits), resolved)
    if len(fraction_digits) > places:
        return None
    fraction_digits = (fraction_digits or "0").ljust(places, "0")
    return Money(int(whole_digits) * (10**places) + int(fraction_digits), resolved)
