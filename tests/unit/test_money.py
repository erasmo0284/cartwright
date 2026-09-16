"""Tests for :mod:`app.core.money`.

The parsing tests double as documentation of the real price strings that
Amazon's markup produces, including the ones that have historically caused
hundredfold errors in scrapers.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.money import (
    Money,
    MoneyError,
    extract_prices,
    parse_money,
    parse_money_ceiling,
    parse_split_price,
)


class TestMoney:
    def test_from_decimal_rounds_half_up(self) -> None:
        assert Money.from_decimal("109.97").cents == 10997
        assert Money.from_decimal("0.005").cents == 1
        assert Money.from_decimal("0.004").cents == 0

    def test_amount_round_trips(self) -> None:
        assert Money(10997).amount == Decimal("109.97")
        assert Money(0).amount == Decimal("0.00")

    def test_zero_decimal_currency(self) -> None:
        yen = Money.from_decimal("1299", "JPY")
        assert yen.cents == 1299
        assert yen.format() == "¥1,299"

    def test_formatting(self) -> None:
        assert Money(10997).format() == "$109.97"
        assert Money(129900).format() == "$1,299.00"
        assert Money(0).format() == "$0.00"
        assert Money(-500).format() == "-$5.00"
        assert Money(10997).format(with_currency_code=True) == "$109.97 USD"

    def test_arithmetic(self) -> None:
        assert (Money(10997) + Money(797)).cents == 11794
        assert (Money(10997) - Money(997)).cents == 10000
        assert (Money(10997) * 3).cents == 32991
        assert (3 * Money(10997)).cents == 32991

    def test_comparisons(self) -> None:
        assert Money(10997) < Money(12000)
        assert Money(10997) <= Money(10997)
        assert Money(12000) > Money(10997)
        assert Money(10997) == Money(10997)
        assert Money(10997) != Money(10998)

    def test_cross_currency_arithmetic_is_refused(self) -> None:
        with pytest.raises(MoneyError, match="deliberately unsupported"):
            Money(100, "USD") + Money(100, "EUR")
        with pytest.raises(MoneyError):
            _ = Money(100, "USD") < Money(100, "GBP")

    def test_float_input_is_refused(self) -> None:
        with pytest.raises(MoneyError):
            Money(109.97)  # type: ignore[arg-type]

    def test_bool_is_not_an_int(self) -> None:
        with pytest.raises(MoneyError):
            Money(True)  # type: ignore[arg-type]

    def test_multiplying_by_non_integer_is_refused(self) -> None:
        with pytest.raises(MoneyError):
            Money(10997) * 1.5  # type: ignore[operator]

    def test_hashable_and_usable_as_dict_key(self) -> None:
        assert len({Money(100), Money(100), Money(200)}) == 2


class TestParseMoney:
    @pytest.mark.parametrize(
        ("text", "expected_cents"),
        [
            ("$109.97", 10997),
            ("$1,299.00", 129900),
            ("109.97", 10997),
            ("$89", 8900),
            ("$0.99", 99),
            ("$1,299", 129900),
            ("  $109.97  ", 10997),
            ("$12,345.67", 1234567),
            # Amazon repeats the same value in an offscreen and a visible span.
            ("$109.97$109.97", 10997),
        ],
    )
    def test_single_price(self, text: str, expected_cents: int) -> None:
        money = parse_money(text)
        assert money is not None
        assert money.cents == expected_cents
        assert money.currency == "USD"

    @pytest.mark.parametrize(
        "text",
        [
            "",
            None,
            "Currently unavailable",
            "Currently unavailable.",
            "See all buying options",
            "Out of Stock",
            "Price not available",
            "no digits at all",
        ],
    )
    def test_no_price(self, text: str | None) -> None:
        assert parse_money(text) is None

    @pytest.mark.parametrize(
        "text",
        [
            # Two genuinely different prices: strict parsing must refuse.
            "$109.97 ($0.92/count)",
            "$109.97 List: $129.99",
            "$10.00 - $20.00",
        ],
    )
    def test_ambiguous_price_is_refused(self, text: str) -> None:
        assert parse_money(text) is None

    def test_european_formatting(self) -> None:
        money = parse_money("1.299,00 €")
        assert money is not None
        assert money.cents == 129900
        assert money.currency == "EUR"

    def test_comma_decimal_with_two_digits(self) -> None:
        money = parse_money("1,29 €")
        assert money is not None
        assert money.cents == 129

    def test_pound_sterling(self) -> None:
        money = parse_money("£49.99")
        assert money is not None
        assert money.cents == 4999
        assert money.currency == "GBP"

    def test_unknown_symbol_falls_back_to_default_currency(self) -> None:
        money = parse_money("49.99", default_currency="CAD")
        assert money is not None
        assert money.currency == "CAD"


class TestParseMoneyCeiling:
    def test_picks_the_largest_value(self) -> None:
        money = parse_money_ceiling("$109.97 List: $129.99")
        assert money is not None
        assert money.cents == 12999

    def test_unit_price_does_not_win(self) -> None:
        money = parse_money_ceiling("$109.97 ($0.92/count)")
        assert money is not None
        assert money.cents == 10997

    def test_mixed_currencies_are_refused(self) -> None:
        assert parse_money_ceiling("$10.00 or €12,00") is None

    def test_no_price_returns_none(self) -> None:
        assert parse_money_ceiling("no numbers here") is None

    def test_ceiling_never_underestimates_a_range(self) -> None:
        money = parse_money_ceiling("$10.00 - $20.00")
        assert money is not None
        assert money.cents == 2000


class TestParseSplitPrice:
    def test_whole_with_trailing_separator(self) -> None:
        money = parse_split_price("109.", "97")
        assert money is not None
        assert money.cents == 10997

    def test_thousands_separator_in_whole_part(self) -> None:
        money = parse_split_price("1,299.", "00")
        assert money is not None
        assert money.cents == 129900

    def test_a_missing_fraction_after_a_separator_is_refused(self) -> None:
        """``"89."`` means a fraction existed and was not read.

        Assuming ``.00`` would under-read by up to 99 cents, in the spending
        direction, which is the one direction this module must never fail in.
        """
        assert parse_split_price("89.", None) is None
        assert parse_split_price("1,299.", "") is None

    def test_a_whole_number_with_no_separator_is_accepted(self) -> None:
        """``"89"`` with no separator is genuinely a whole amount."""
        money = parse_split_price("89", None)
        assert money is not None
        assert money.cents == 8900

    def test_single_digit_fraction_is_padded(self) -> None:
        money = parse_split_price("89.", "5")
        assert money is not None
        assert money.cents == 8950

    def test_overlong_fraction_is_refused(self) -> None:
        assert parse_split_price("89.", "5000") is None

    def test_missing_whole_is_refused(self) -> None:
        assert parse_split_price(None, "97") is None
        assert parse_split_price("", "97") is None

    def test_naive_concatenation_bug_is_not_reproduced(self) -> None:
        """``"109." + "97"`` must not become $10,997.00."""
        money = parse_split_price("109.", "97")
        assert money is not None
        assert money.cents != 1099700


class TestExtractPrices:
    def test_order_is_preserved_and_duplicates_collapsed(self) -> None:
        prices = extract_prices("$5.00 $9.99 $5.00")
        assert [price.cents for price in prices] == [500, 999]

    def test_empty_text(self) -> None:
        assert extract_prices("") == []
        assert extract_prices(None) == []
