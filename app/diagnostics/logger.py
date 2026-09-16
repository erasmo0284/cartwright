"""Application logging.

Two sinks are configured:

* ``logs/app.log`` -- rotating, human-readable, for support and debugging.
* ``logs/events.jsonl`` -- rotating, one JSON object per line, for the
  diagnostic report and for machine inspection of automation runs.

Every record passes through :class:`SanitizingFilter`, which applies
:mod:`app.diagnostics.redaction` to the formatted message and to structured
fields. Sanitising at the handler boundary rather than at each call site means
a careless ``logger.debug(page_content)`` somewhere cannot leak a session
cookie.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path
from typing import Any, Final, Mapping

from app.diagnostics.redaction import redact_mapping, redact_text
from app.version import VERSION

#: Rotation policy. Five 2 MiB files is enough to cover several days of
#: monitoring without letting the log directory grow without bound.
MAX_BYTES: Final = 2 * 1024 * 1024
BACKUP_COUNT: Final = 5

TEXT_LOG_NAME: Final = "app.log"
EVENT_LOG_NAME: Final = "events.jsonl"

_LOG_FORMAT: Final = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"
_DATE_FORMAT: Final = "%Y-%m-%d %H:%M:%S"

#: Record attributes that :class:`logging.LogRecord` always defines; anything
#: else on a record is treated as a caller-supplied structured field.
_STANDARD_ATTRS: Final[frozenset[str]] = frozenset(
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

_configured = False
_configured_lock = threading.Lock()


class SanitizingFilter(logging.Filter):
    """Redact a record's message and structured fields in place.

    Implemented as a filter rather than a formatter so that both the text and
    JSONL handlers benefit without either being able to bypass it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            rendered = str(record.msg)
        record.msg = redact_text(rendered)
        record.args = ()

        for key in list(record.__dict__):
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            value = record.__dict__[key]
            if isinstance(value, Mapping):
                record.__dict__[key] = redact_mapping(value)
            elif isinstance(value, str):
                record.__dict__[key] = redact_text(value)

        if record.exc_info and record.exc_info[1] is not None:
            # The traceback text is rendered by the formatter; pre-redact the
            # exception's own message, which is the part that can carry data.
            exc = record.exc_info[1]
            try:
                exc.args = tuple(
                    redact_text(arg) if isinstance(arg, str) else arg
                    for arg in exc.args
                )
            except Exception:  # pragma: no cover - some exceptions are frozen
                pass
        return True


class TextFormatter(logging.Formatter):
    """Human-readable format with structured fields appended as ``k=v``."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = _structured_fields(record)
        if extras:
            pairs = " ".join(f"{key}={_compact(value)}" for key, value in extras.items())
            return f"{base} | {pairs}"
        return base


class JsonlFormatter(logging.Formatter):
    """One JSON object per record, for the diagnostic report."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "thread": record.threadName,
        }
        payload.update(_structured_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, default=str, ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return json.dumps(
                {"ts": payload["ts"], "level": payload["level"], "message": "unserialisable record"}
            )


def _structured_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_ATTRS and not key.startswith("_")
    }


def _compact(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) > 300:
        text = f"{text[:297]}..."
    return text if " " not in text else f'"{text}"'


def setup_logging(
    logs_dir: Path,
    *,
    level: int | str = logging.INFO,
    console: bool | None = None,
) -> logging.Logger:
    """Configure the root logger. Safe to call more than once.

    ``console`` defaults to writing to stderr only when a console exists,
    which is true when running from source but not in a windowed PyInstaller
    build (where ``sys.stderr`` may be ``None``).
    """
    global _configured
    with _configured_lock:
        root = logging.getLogger()
        if _configured:
            root.setLevel(_coerce_level(level))
            return logging.getLogger("app")

        logs_dir.mkdir(parents=True, exist_ok=True)
        root.setLevel(_coerce_level(level))
        for handler in list(root.handlers):
            root.removeHandler(handler)

        sanitiser = SanitizingFilter()

        text_handler = logging.handlers.RotatingFileHandler(
            logs_dir / TEXT_LOG_NAME,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        text_handler.setFormatter(TextFormatter(_LOG_FORMAT, _DATE_FORMAT))
        text_handler.addFilter(sanitiser)
        root.addHandler(text_handler)

        event_handler = logging.handlers.RotatingFileHandler(
            logs_dir / EVENT_LOG_NAME,
            maxBytes=MAX_BYTES,
            backupCount=2,
            encoding="utf-8",
            delay=True,
        )
        event_handler.setFormatter(JsonlFormatter())
        event_handler.addFilter(sanitiser)
        event_handler.setLevel(logging.INFO)
        root.addHandler(event_handler)

        if console is None:
            console = sys.stderr is not None and sys.stderr.isatty()
        if console:
            stream_handler = logging.StreamHandler(sys.stderr)
            stream_handler.setFormatter(TextFormatter(_LOG_FORMAT, _DATE_FORMAT))
            stream_handler.addFilter(sanitiser)
            root.addHandler(stream_handler)

        # Third-party chatter that is never useful at INFO.
        logging.getLogger("asyncio").setLevel(logging.WARNING)
        logging.getLogger("PIL").setLevel(logging.WARNING)

        _configured = True

    logger = logging.getLogger("app")
    logger.info(
        "Logging started",
        extra={
            "version": VERSION,
            "python": sys.version.split()[0],
            "frozen": bool(getattr(sys, "frozen", False)),
            "pid": os.getpid(),
            "logs_dir": str(logs_dir),
        },
    )
    return logger


def _coerce_level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelNamesMapping().get(str(level).upper())
    return resolved if resolved is not None else logging.INFO


def reset_logging() -> None:
    """Tear down handlers so a test can reconfigure logging."""
    global _configured
    with _configured_lock:
        root = logging.getLogger()
        for handler in list(root.handlers):
            handler.close()
            root.removeHandler(handler)
        _configured = False


def get_logger(name: str) -> logging.Logger:
    """A namespaced logger. ``name`` is appended under the ``app`` root."""
    return logging.getLogger(name if name.startswith("app") else f"app.{name}")


def log_paths(logs_dir: Path) -> tuple[Path, Path]:
    """The text and event log paths, for the diagnostics UI."""
    return logs_dir / TEXT_LOG_NAME, logs_dir / EVENT_LOG_NAME


def clear_old_logs(logs_dir: Path) -> int:
    """Delete rotated log files, keeping the two live ones. Returns the count.

    Exposed in Settings -> Diagnostics so a user can reclaim space without
    touching anything else in the data directory.
    """
    removed = 0
    for path in logs_dir.glob("*.log.*"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    for path in logs_dir.glob("*.jsonl.*"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed
