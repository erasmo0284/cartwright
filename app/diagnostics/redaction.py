"""Redaction of sensitive values from anything the app writes to disk.

This module is the single implementation used by the logger, by automation
diagnostics snapshots and by the exported diagnostic report. Keeping it in
one place means the security review has one thing to audit, and the test
suite can assert that no known-sensitive shape survives.

The rules err towards over-redaction: a log line that is harder to read is a
cheap price for never leaking a session cookie or a card number. The one
deliberate exception is the Amazon order-number shape, which is preserved
because verifying an order is the whole point of the confirmation step.
"""

from __future__ import annotations

import re
from typing import Any, Final, Mapping

REDACTED: Final = "[redacted]"

#: Names of Amazon (and generic) cookies / fields that authenticate a session
#: or carry payment material. Matched as case-insensitive substrings of a key.
SENSITIVE_KEY_FRAGMENTS: Final[tuple[str, ...]] = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "cookie",
    "authorization",
    "session-id",
    "session_id",
    "sessionid",
    "ubid-main",
    "ubid-acb",
    "at-main",
    "at-acb",
    "sess-at-main",
    "sst-main",
    "x-amz-",
    "csrf",
    "otpcode",
    "otp-code",
    "mfa",
    "totp",
    "cvv",
    "cvc",
    "cardnumber",
    "card-number",
    "card_number",
    "addcreditcard",
    "apikey",
    "api-key",
    "api_key",
    "access-key",
    "private-key",
    "bearer",
    "credential",
    "passkey",
    "claimed-id",
    "openid",
)

_KEY_FRAGMENT_ALTERNATION: Final = "|".join(
    re.escape(fragment) for fragment in SENSITIVE_KEY_FRAGMENTS
)

#: ``"key": "value"`` where the key name is sensitive (JSON-ish text).
_JSON_KEY_VALUE = re.compile(
    rf'(?P<prefix>"[^"\n]*(?:{_KEY_FRAGMENT_ALTERNATION})[^"\n]*"\s*:\s*)'
    r'(?P<value>"[^"\n]*"|null|true|false|-?\d+(?:\.\d+)?)',
    re.IGNORECASE,
)

#: ``key=value`` in a query string, form body or ``repr()`` output.
_ASSIGNED_KEY_VALUE = re.compile(
    rf"(?P<prefix>[\w.\-\[\]]*(?:{_KEY_FRAGMENT_ALTERNATION})[\w.\-\[\]]*\s*=\s*)"
    r"""(?P<value>"[^"\n]*"|'[^'\n]*'|[^&;\s,)\]}"']+)""",
    re.IGNORECASE,
)

#: ``Header-Name: value`` where the header name is sensitive.
_HEADER_KEY_VALUE = re.compile(
    rf"(?P<prefix>^[ \t]*[\w.\-]*(?:{_KEY_FRAGMENT_ALTERNATION})[\w.\-]*[ \t]*:[ \t]*)"
    r"(?P<value>[^\n]+)",
    re.IGNORECASE | re.MULTILINE,
)

#: A contiguous run of digits long enough to be a payment card.
_CARD_CONTIGUOUS = re.compile(r"(?<![\d\-.])\d{13,19}(?![\d\-.])")

#: A card written in separated groups, e.g. ``4111 1111 1111 1111``.
_CARD_GROUPED = re.compile(
    r"(?<![\d\-.])\d{4}[ \-]\d{4}[ \-]\d{4}[ \-]\d{1,7}(?![\d\-.])"
)

#: An Amazon order number. Preserved: it is what a user needs to verify an
#: order, and it grants no access on its own.
_ORDER_NUMBER = re.compile(r"^\d{3}-\d{7}-\d{7}$")

#: A JWT-shaped bearer token.
_JWT_LIKE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]*)?")

#: Amazon sign-in and verification flows put single-use material in the query
#: string of these paths, so their URLs are logged as the path alone.
_SENSITIVE_URL_PATHS: Final[tuple[str, ...]] = (
    "/ap/signin",
    "/ap/mfa",
    "/ap/cvf",
    "/ap/challenge",
    "/ap/dcq",
    "/ap/forgotpassword",
    "/errors/validatecaptcha",
    "/gp/aw/si",
)


def _redact_card(match: re.Match[str]) -> str:
    text = match.group(0)
    if _ORDER_NUMBER.match(text):
        return text
    return REDACTED


def redact_text(value: str | None) -> str:
    """Return ``value`` with sensitive substrings replaced.

    Handles key/value pairs whose key name is sensitive, JWT-shaped tokens and
    card-shaped digit runs.
    """
    if value is None:
        return ""
    if not value:
        return value

    text = value
    for pattern in (_JSON_KEY_VALUE, _ASSIGNED_KEY_VALUE, _HEADER_KEY_VALUE):
        text = pattern.sub(lambda m: f"{m.group('prefix')}{REDACTED}", text)
    text = _JWT_LIKE.sub(REDACTED, text)
    text = _CARD_GROUPED.sub(_redact_card, text)
    text = _CARD_CONTIGUOUS.sub(_redact_card, text)
    return text


def redact_url(url: str | None) -> str:
    """Reduce a URL to something safe to log.

    Scheme, host and path are kept because they are what makes a log useful
    ("we were on the checkout page"), while the query string is always dropped
    -- Amazon puts session and continuation material there.
    """
    if not url:
        return ""
    without_fragment = url.split("#", 1)[0]
    base, separator, _query = without_fragment.partition("?")
    if separator:
        return f"{base}?{REDACTED}"
    return base


def is_sensitive_key(key: str) -> bool:
    """True when a mapping key's name alone marks its value as sensitive."""
    lowered = str(key).lower()
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def redact_mapping(data: Mapping[str, Any], *, _depth: int = 0) -> dict[str, Any]:
    """Recursively redact a mapping destined for a log or a report.

    Values under a sensitive key are replaced outright; other string values go
    through :func:`redact_text`, and keys whose name mentions a URL go through
    :func:`redact_url`.
    """
    if _depth > 8:
        return {"truncated": True}

    result: dict[str, Any] = {}
    for key, value in data.items():
        name = str(key)
        if is_sensitive_key(name):
            result[name] = REDACTED
        elif isinstance(value, Mapping):
            result[name] = redact_mapping(value, _depth=_depth + 1)
        elif isinstance(value, (list, tuple, set)):
            result[name] = [_redact_item(item, _depth + 1) for item in value]
        elif isinstance(value, str):
            result[name] = (
                redact_url(value) if "url" in name.lower() else redact_text(value)
            )
        else:
            result[name] = value
    return result


def _redact_item(item: Any, depth: int) -> Any:
    if isinstance(item, Mapping):
        return redact_mapping(item, _depth=depth)
    if isinstance(item, str):
        return redact_text(item)
    return item
