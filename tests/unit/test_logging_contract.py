"""Static checks on how the codebase logs.

These exist because of a real bug: ``logger.info(..., extra={"name": ...})``
raises ``KeyError`` inside the logging module, but only once logging is
actually enabled at that level. With the default root level of WARNING the
call is skipped entirely, so the crash hid until the application configured
its own logging -- which is to say, it would have appeared for a user and
never for a developer running the tests.

A static scan is the right shape of test here: it covers every call site at
once, including ones nobody thought to exercise.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent.parent / "app"

#: Attributes ``logging.LogRecord`` always defines. Passing any of them
#: through ``extra`` makes ``Logger.makeRecord`` raise.
RESERVED_RECORD_ATTRIBUTES = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def _python_files() -> list[Path]:
    return sorted(
        path
        for path in APP_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _extra_keys(path: Path) -> list[tuple[int, str]]:
    """Every literal key passed as ``extra={...}`` in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                continue
            for key in keyword.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    found.append((node.lineno, key.value))
    return found


def test_reserved_attributes_are_never_used_in_extra() -> None:
    """A reserved key in ``extra`` is a crash waiting for a log level change."""
    offences: list[str] = []
    for path in _python_files():
        for line, key in _extra_keys(path):
            if key in RESERVED_RECORD_ATTRIBUTES:
                offences.append(
                    f"{path.relative_to(APP_ROOT.parent)}:{line} uses "
                    f"extra={{{key!r}: ...}}, which LogRecord reserves"
                )
    assert not offences, "\n".join(offences)


def test_the_reserved_list_matches_reality() -> None:
    """Guard the list above against a change in the logging module."""
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="message", args=(), exc_info=None,
    )
    actual = set(record.__dict__)
    # ``asctime`` and ``message`` are added by the formatter rather than the
    # constructor, but logging rejects them in ``extra`` all the same.
    missing = actual - RESERVED_RECORD_ATTRIBUTES
    assert not missing, (
        "LogRecord gained attributes that the reserved list does not cover: "
        f"{sorted(missing)}"
    )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_every_extra_key_survives_a_real_log_call(path: Path) -> None:
    """Prove each key is actually accepted, not just absent from a list."""
    logger = logging.getLogger("app.tests.logging_contract")
    logger.setLevel(logging.DEBUG)
    keys = {key for _line, key in _extra_keys(path)}
    if not keys:
        pytest.skip("no structured logging in this module")
    # A single call with every key this module uses; raises if any collide.
    logger.debug("contract check", extra={key: "value" for key in keys})
