"""Tests for :mod:`app.diagnostics.redaction`.

These are security tests: each case is a shape of sensitive data that must
never survive into a log file, a screenshot sidecar or an exported report.
"""

from __future__ import annotations

import pytest

from app.diagnostics.redaction import (
    REDACTED,
    redact_mapping,
    redact_text,
    redact_url,
)

SENSITIVE_SAMPLES = [
    "session-token=Atza|IwEBIExampleTokenValue123456",
    "session-id=143-1234567-7654321",
    'Cookie: session-id=abc; ubid-main=def; at-main=Atza|xyz',
    "Set-Cookie: sess-at-main=\"AbCdEf==\"; Domain=.amazon.com",
    "authorization: Bearer abcdefghijklmnop",
    'password="hunter2"',
    "password=hunter2&email=someone@example.com",
    '{"session-token": "AbCdEf123456"}',
    '{"password": "hunter2"}',
    "x-amz-access-token=Atza|Example",
    "anti-csrftoken-a2z=gJc0abcdef",
    "otpCode=123456",
    "mfaCode=987654",
    "cvv=123",
    "cardNumber=4111111111111111",
    "4111 1111 1111 1111",
    "4111-1111-1111-1111",
    "4111111111111111",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdef",
    "apiKey=sk-abcdef123456",
    "passkey=somelongpasskeyvalue",
]


class TestRedactText:
    @pytest.mark.parametrize("sample", SENSITIVE_SAMPLES)
    def test_secret_value_does_not_survive(self, sample: str) -> None:
        cleaned = redact_text(sample)
        assert REDACTED in cleaned, f"nothing redacted in {sample!r} -> {cleaned!r}"
        for leaked in (
            "hunter2",
            "AbCdEf123456",
            "4111111111111111",
            "4111 1111 1111 1111",
            "4111-1111-1111-1111",
            "abcdefghijklmnop",
            "sk-abcdef123456",
            "somelongpasskeyvalue",
            "123456",
            "987654",
        ):
            assert leaked not in cleaned, f"{leaked!r} leaked from {sample!r}"

    def test_amazon_order_number_is_preserved(self) -> None:
        """Order numbers must stay readable: they are how a user verifies."""
        text = "Order placed: 112-1234567-7654321"
        assert redact_text(text) == text

    def test_ordinary_text_is_untouched(self) -> None:
        for text in (
            "Reading price from #corePrice_feature_div",
            "Seller: Amazon.com, ships from Amazon.com",
            "Order total $117.94 within limit $125.00",
            "quantity=1",
            "asin=B07XYZ1234",
            "Checked product in 2.4s",
        ):
            assert redact_text(text) == text

    def test_empty_and_none(self) -> None:
        assert redact_text(None) == ""
        assert redact_text("") == ""

    def test_timestamps_are_not_mistaken_for_cards(self) -> None:
        # A 13-digit epoch-milliseconds value is card-length; accept that it
        # is redacted rather than risk the reverse, but a price must not be.
        assert redact_text("total=11794") == "total=11794"
        assert redact_text("$1,299.00") == "$1,299.00"


class TestRedactUrl:
    def test_query_string_is_always_dropped(self) -> None:
        assert redact_url(
            "https://www.amazon.com/dp/B07XYZ1234?ref=abc&pd_rd_r=secret"
        ) == f"https://www.amazon.com/dp/B07XYZ1234?{REDACTED}"

    def test_path_is_kept(self) -> None:
        assert (
            redact_url("https://www.amazon.com/gp/buy/spc/handlers/display.html")
            == "https://www.amazon.com/gp/buy/spc/handlers/display.html"
        )

    def test_signin_url_query_is_dropped(self) -> None:
        cleaned = redact_url(
            "https://www.amazon.com/ap/signin?openid.claimed_id=secretvalue"
        )
        assert "secretvalue" not in cleaned
        assert cleaned.startswith("https://www.amazon.com/ap/signin")

    def test_fragment_is_dropped(self) -> None:
        assert redact_url("https://a.com/p#token=abc") == "https://a.com/p"

    def test_empty(self) -> None:
        assert redact_url(None) == ""
        assert redact_url("") == ""


class TestRedactMapping:
    def test_sensitive_keys_are_replaced(self) -> None:
        result = redact_mapping(
            {
                "asin": "B07XYZ1234",
                "password": "hunter2",
                "session-token": "abc123",
                "cookies": [{"name": "at-main", "value": "Atza|x"}],
            }
        )
        assert result["asin"] == "B07XYZ1234"
        assert result["password"] == REDACTED
        assert result["session-token"] == REDACTED
        assert result["cookies"] == REDACTED

    def test_nested_mapping(self) -> None:
        result = redact_mapping(
            {"checkout": {"total": "$117.94", "payment_token": "abc"}}
        )
        assert result["checkout"]["total"] == "$117.94"
        assert result["checkout"]["payment_token"] == REDACTED

    def test_url_keys_use_url_redaction(self) -> None:
        result = redact_mapping({"url": "https://a.com/p?session-id=abc"})
        assert "abc" not in result["url"]

    def test_non_string_values_pass_through(self) -> None:
        result = redact_mapping({"quantity": 3, "ok": True, "price_cents": 10997})
        assert result == {"quantity": 3, "ok": True, "price_cents": 10997}

    def test_deep_recursion_is_bounded(self) -> None:
        deep: dict[str, object] = {"leaf": 1}
        for _ in range(30):
            deep = {"nested": deep}
        result = redact_mapping(deep)
        # Must terminate and mark truncation rather than recursing forever.
        flat = repr(result)
        assert "truncated" in flat

    def test_list_of_strings_is_redacted(self) -> None:
        result = redact_mapping({"steps": ["ok", "password=hunter2"]})
        assert result["steps"][0] == "ok"
        assert "hunter2" not in result["steps"][1]
