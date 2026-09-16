"""Time handling.

Every timestamp the application persists is a UTC ISO-8601 string with
microsecond precision and a ``Z`` suffix. That format sorts correctly as
text, which lets SQLite order and compare timestamps without a date type,
and it stays readable in the database and the logs.

Local time is used only for display.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

#: ``2026-09-16T14:05:09.123456Z`` -- fixed width, so text sort == time sort.
ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def to_iso(moment: datetime) -> str:
    """Serialise a datetime to the canonical storage format.

    Naive datetimes are assumed to be UTC rather than rejected, because a
    naive value reaching here is a bug that should not lose the user's data.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime(ISO_FORMAT)


def from_iso(text: str | None) -> datetime | None:
    """Parse a stored timestamp. Returns ``None`` for empty input.

    Tolerates values without microseconds and with a ``+00:00`` offset so
    that hand-edited rows and older formats still load.
    """
    if not text:
        return None
    candidate = text.strip()
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def now_iso() -> str:
    """Shorthand for ``to_iso(utcnow())``."""
    return to_iso(utcnow())


def iso_in(seconds: float) -> str:
    """A stored timestamp ``seconds`` from now. Used for scheduling."""
    return to_iso(utcnow() + timedelta(seconds=seconds))


def to_local(moment: datetime) -> datetime:
    """Convert a UTC datetime to the machine's local timezone for display."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone()


def format_clock(moment: datetime | None) -> str:
    """Local wall-clock time, e.g. ``2:14 PM``. Empty string for ``None``."""
    if moment is None:
        return ""
    local = to_local(moment)
    return local.strftime("%I:%M %p").lstrip("0")


def format_day(moment: datetime | None) -> str:
    """A day heading for the activity feed: ``Today``/``Yesterday``/date."""
    if moment is None:
        return ""
    local_date = to_local(moment).date()
    today = to_local(utcnow()).date()
    delta = (today - local_date).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if 0 < delta < 7:
        return to_local(moment).strftime("%A")
    return to_local(moment).strftime("%B %d, %Y").replace(" 0", " ")


def format_relative(moment: datetime | None, *, reference: datetime | None = None) -> str:
    """A short relative description such as ``2 min ago`` or ``in 4 min``.

    Used for "last checked" and "next check" in the watch list, where an
    absolute timestamp reads as noise.
    """
    if moment is None:
        return "never"
    now = reference or utcnow()
    seconds = (moment - now).total_seconds()
    future = seconds > 0
    magnitude = abs(seconds)

    if magnitude < 10:
        return "just now" if not future else "any moment"
    if magnitude < 60:
        text = f"{int(magnitude)} sec"
    elif magnitude < 3600:
        text = f"{int(magnitude // 60)} min"
    elif magnitude < 86400:
        hours = int(magnitude // 3600)
        text = f"{hours} hour" if hours == 1 else f"{hours} hours"
    else:
        days = int(magnitude // 86400)
        text = f"{days} day" if days == 1 else f"{days} days"
    return f"in {text}" if future else f"{text} ago"


def format_duration(seconds: float) -> str:
    """A settings-friendly interval label, e.g. ``5 minutes``, ``1 hour``."""
    total = int(round(seconds))
    if total < 60:
        return f"{total} seconds"
    if total < 3600:
        minutes = total // 60
        return "1 minute" if minutes == 1 else f"{minutes} minutes"
    if total % 3600 == 0:
        hours = total // 3600
        return "1 hour" if hours == 1 else f"{hours} hours"
    hours, remainder = divmod(total, 3600)
    return f"{hours}h {remainder // 60}m"
