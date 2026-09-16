"""Checks on the source itself, for damage no ordinary test would notice.

One of these is not hypothetical. A patch applied through a shell heredoc
turned the ``\\b`` word boundaries in the checkout quantity patterns into
literal backspace characters (``0x08``). The module still imported, the
regexes still compiled, and every pattern silently stopped matching -- so a
checkout line reading "Qty: 2" reported *no readable quantity*. The tests
that covered it were themselves asserting the broken behaviour, so nothing
went red.

A literal control character in source is never intentional here, and a
regex that no longer matches the wording it was written for is worth a test
of its own.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.automation import selectors

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Directories that are not our source.
SKIP_PARTS = {".venv", "build", "dist", "__pycache__", ".git", ".pytest_cache"}

#: Control bytes that no source file in this project has a reason to contain.
#: Tab (0x09), newline (0x0a) and carriage return (0x0d) are excluded.
FORBIDDEN_BYTES = {
    0x00: "NUL",
    0x07: "BEL (a mangled \\a)",
    0x08: "BACKSPACE (a mangled \\b)",
    0x0B: "VERTICAL TAB (a mangled \\v)",
    0x0C: "FORM FEED (a mangled \\f)",
    0x1B: "ESCAPE (a mangled \\e)",
}


def source_files() -> list[Path]:
    found: list[Path] = []
    for pattern in ("*.py", "*.md", "*.iss", "*.spec", "*.txt", "*.ini"):
        for path in PROJECT_ROOT.rglob(pattern):
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            found.append(path)
    return found


def test_the_scan_actually_sees_the_source() -> None:
    """A guard against the guard silently scanning nothing."""
    names = {path.name for path in source_files()}
    assert "selectors.py" in names
    assert "purchase_guard.py" in names
    assert len(names) > 50


def test_no_source_file_contains_a_stray_control_character() -> None:
    offenders: list[str] = []
    for path in source_files():
        data = path.read_bytes()
        for value, description in FORBIDDEN_BYTES.items():
            if bytes([value]) in data:
                line = data.count(b"\n", 0, data.index(bytes([value]))) + 1
                offenders.append(
                    f"{path.relative_to(PROJECT_ROOT)}:{line} contains {description}"
                )
    assert not offenders, "\n".join(offenders)


class TestCheckoutQuantityWordings:
    """Every wording the patterns exist for must actually match.

    Compiling is not matching: this is what a mangled escape breaks.
    """

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Klein Tools Clamp Meter $109.97 Qty: 2", 2),
            ("Clamp Meter $109.97 Qty 3", 3),
            ("Clamp Meter Quantity: 4", 4),
            ("Clamp Meter quantity 5", 5),
            ("2 x $10.00", 2),
            ("3 × $10.00", 3),
        ],
    )
    def test_a_known_wording_is_read(self, text: str, expected: int) -> None:
        for pattern in selectors.CHECKOUT_QUANTITY_PATTERNS:
            match = pattern.search(text)
            if match:
                assert int(match.group(1)) == expected
                return
        raise AssertionError(f"no pattern matched {text!r}")

    @pytest.mark.parametrize(
        "text",
        [
            # "antiquity" must not be read as a quantity of 7.
            "Antiquity Collection 7 Piece Set",
            "A product with no quantity wording at all $10.00",
        ],
    )
    def test_an_unrelated_string_is_not_read_as_a_quantity(self, text: str) -> None:
        for pattern in selectors.CHECKOUT_QUANTITY_PATTERNS:
            assert pattern.search(text) is None, pattern.pattern

    def test_the_patterns_use_word_boundaries_not_control_characters(self) -> None:
        """The specific damage that made every one of them stop matching."""
        for pattern in selectors.CHECKOUT_QUANTITY_PATTERNS:
            assert "\x08" not in pattern.pattern
            assert not re.search(r"[\x00-\x08\x0b\x0c\x1b]", pattern.pattern)
