"""The support bundle the user can hand over, and what it must never contain.

A user asking for help should be able to send one file that answers "what is
this installation actually like?" without having to trust that the tool is
not also sending their Amazon session. That trust is the whole design
constraint here, so this module is written to be auditable:

*Everything is assembled, then redacted.* The finished structure goes through
:func:`app.diagnostics.redaction.redact_mapping` before it is returned, so a
field added later cannot skip the filter by forgetting to call it. Log lines
are redacted a second time on the way out, even though the logger already
redacts on the way in, because a bundle is the wrong place to rely on that.

*The browser profile is never walked.* It holds the sign-in cookies. Its
*path* appears in the report, because "where does the profile live" is a
useful answer, but no file inside :attr:`app.paths.AppPaths.browser_dir` is
ever read or archived, and :func:`export_zip` refuses any candidate under it.

*Nothing is quietly dropped.* The report carries an ``_excluded`` list naming
every category deliberately left out and why. A privacy claim the user cannot
check is not worth making, and someone reading the JSON should be able to see
what is missing rather than wonder.

*Screenshots are listed, not included.* An automation screenshot is a picture
of a signed-in Amazon page. The file names go in the report so support can
ask for a specific one; the images stay on the user's computer.
"""

from __future__ import annotations

import json
import platform
import sys
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from app.branding import BRAND
from app.core.timeutil import now_iso
from app.diagnostics.health import HealthReport, HealthService
from app.diagnostics.logger import EVENT_LOG_NAME, TEXT_LOG_NAME
from app.diagnostics.redaction import redact_mapping, redact_text
from app.paths import is_frozen
from app.version import BUILD_CHANNEL, VERSION

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.automation.browser_manager import BrowserManager
    from app.config import SettingsService
    from app.database.database import Database
    from app.database.repositories.activity import ActivityRepository
    from app.notifications.notifier import Notifier
    from app.paths import AppPaths

#: How many recent event-log lines the report carries. Enough to cover the
#: last few monitoring cycles without making the file awkward to open.
DEFAULT_LOG_LINES: Final = 200

#: Name the JSON report is given inside the exported archive.
REPORT_NAME_IN_ZIP: Final = "report.json"

#: Folder the logs are placed in inside the exported archive.
LOGS_FOLDER_IN_ZIP: Final = "logs"

#: Settings fields stripped from the report. The account label is the user's
#: Amazon name or email address, which support does not need in order to read
#: a diagnostic bundle.
REMOVED_SETTING_KEYS: Final[tuple[str, ...]] = ("amazon_account_label",)

#: What is deliberately left out, and why. Written into the report under
#: ``_excluded`` so the user can see the tool is not hiding anything.
EXCLUDED_ITEMS: Final[tuple[dict[str, str], ...]] = (
    {
        "item": "Browser profile folder",
        "why": (
            "It holds the sign-in data for your Amazon account. Its location "
            "is named in this report but none of its files are read or "
            "included."
        ),
    },
    {
        "item": "Sign-in data and access tokens",
        "why": "They would let someone else use your Amazon account.",
    },
    {
        "item": "Passwords and one-time codes",
        "why": (
            "The app never stores them, and anything shaped like one is "
            "replaced before it reaches a log file."
        ),
    },
    {
        "item": "Payment card details and delivery addresses",
        "why": (
            "The app never reads or keeps them. Amazon holds them and the app "
            "only checks that one of each is selected."
        ),
    },
    {
        "item": "Everything after the question mark in a web address",
        "why": (
            "Amazon puts sign-in material there, so only the page itself is "
            "recorded."
        ),
    },
    {
        "item": "Saved screenshots",
        "why": (
            "They are pictures of your signed-in Amazon pages. Only the file "
            "names are listed, so support can ask you for one."
        ),
    },
    {
        "item": "Your Amazon account name",
        "why": "It is not needed to work out what went wrong.",
    },
)


def build_report(
    *,
    paths: AppPaths | None = None,
    database: Database | None = None,
    settings: SettingsService | None = None,
    activity: ActivityRepository | None = None,
    browser_manager: BrowserManager | None = None,
    notifier: Notifier | None = None,
    health: HealthReport | None = None,
    log_lines: int = DEFAULT_LOG_LINES,
) -> dict[str, Any]:
    """Assemble the diagnostic report.

    Every dependency is optional, because a report is most useful when
    something failed to start; a missing dependency leaves its section out
    rather than failing the whole export. ``health`` may be supplied when the
    caller has already run the checks, so the screen and the export agree.

    The returned structure has already been redacted.
    """
    if health is None:
        health = HealthService(
            paths=paths,
            database=database,
            settings=settings,
            browser_manager=browser_manager,
            notifier=notifier,
        ).run()

    report: dict[str, Any] = {
        "generated_at": now_iso(),
        "app": {
            "name": BRAND.display_name,
            "version": VERSION,
            "build_channel": BUILD_CHANNEL,
            "frozen": is_frozen(),
            "aumid": BRAND.aumid,
        },
        "system": {
            "os": platform.platform(),
            "python": sys.version.split()[0],
            "architecture": platform.machine(),
        },
        "health": health.to_dict(),
        "_excluded": [dict(item) for item in EXCLUDED_ITEMS],
    }

    if paths is not None:
        report["folders"] = {
            "root": str(paths.root),
            "logs": str(paths.logs_dir),
            "screenshots": str(paths.screenshots_dir),
            "backups": str(paths.backups_dir),
            "browser_profile": str(paths.browser_profile_dir),
        }
        report["recent_log"] = _tail_lines(paths.logs_dir / EVENT_LOG_NAME, log_lines)

    if browser_manager is not None:
        report["browser"] = _browser_section(browser_manager)

    if database is not None:
        report["database"] = _database_section(database)
        report["jobs"] = _jobs_section(database)

    if settings is not None:
        report["settings"] = _settings_section(settings)

    if activity is not None:
        report["diagnostic_captures"] = _capture_names(activity)

    return redact_mapping(report)


def write_report(destination: Path, **deps: Any) -> Path:
    """Write the report to ``destination`` as indented JSON.

    ``deps`` are passed straight to :func:`build_report`. Returns the path
    written, so a caller can show it to the user.
    """
    report = build_report(**deps)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_render(report), encoding="utf-8")
    return destination


def export_zip(destination: Path, **deps: Any) -> Path:
    """Write a support archive holding the report and the rotated logs.

    ``deps`` are passed straight to :func:`build_report`. Only the JSON report
    and files from the logs directory are archived; :func:`_is_excluded_path`
    keeps anything under the browser directory out even if the logs directory
    were ever redirected there.
    """
    paths: AppPaths | None = deps.get("paths")
    report = build_report(**deps)

    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(REPORT_NAME_IN_ZIP, _render(report))
        if paths is not None:
            for log_file in _log_files(paths):
                archive.writestr(
                    f"{LOGS_FOLDER_IN_ZIP}/{log_file.name}",
                    redact_text(_read_text(log_file)),
                )
    return destination


def clear_diagnostics(paths: AppPaths, activity: ActivityRepository) -> int:
    """Delete every saved screenshot and its row. Returns the files removed.

    The stored file names are reduced to their last component before being
    joined to the screenshots directory, so a stored value can never point
    the deletion at something outside it. Files in the directory with no row
    are removed too, because they are the residue of a crash and the user
    asked for the screenshots to be gone.
    """
    recorded = activity.clear_diagnostics()
    removed = 0
    seen: set[Path] = set()

    for name in recorded:
        candidate = paths.screenshots_dir / Path(str(name)).name
        if candidate in seen:
            continue
        seen.add(candidate)
        if _unlink(candidate):
            removed += 1

    if paths.screenshots_dir.is_dir():
        for orphan in sorted(paths.screenshots_dir.iterdir()):
            if orphan in seen or not orphan.is_file():
                continue
            seen.add(orphan)
            if _unlink(orphan):
                removed += 1

    return removed


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _browser_section(browser_manager: BrowserManager) -> dict[str, Any]:
    try:
        info = browser_manager.info()
    except Exception as exc:  # noqa: BLE001 - the report must still be produced
        return {"error": f"Could not inspect the browser ({exc.__class__.__name__})"}
    try:
        profile_bytes = browser_manager.profile_size_bytes()
    except OSError:
        profile_bytes = 0
    return {
        "installed": info.installed,
        "summary": info.summary,
        "version": info.version,
        "executable_path": info.executable_path,
        "browsers_dir": info.browsers_dir,
        "profile_dir": info.profile_dir,
        "profile_size_bytes": profile_bytes,
        "playwright_version": info.playwright_version,
        "running": info.running,
    }


def _database_section(database: Database) -> dict[str, Any]:
    intact, result = database.integrity_check()
    return {
        "file": database.path.name,
        "size_bytes": database.file_size_bytes(),
        "schema_version": database.current_version(),
        "schema_target": database.target_version(),
        "integrity_ok": intact,
        "integrity_result": result,
        "row_counts": _row_counts(database),
    }


def _row_counts(database: Database) -> dict[str, int]:
    """One count per table. Table names come from SQLite, not a hard-coded
    list, so a table added by a future migration is reported without anyone
    remembering to update this module.
    """
    counts: dict[str, int] = {}
    try:
        rows = database.query_all(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    except Exception:  # noqa: BLE001
        return counts
    for row in rows:
        table = str(row["name"])
        try:
            counts[table] = int(
                database.query_scalar(f'SELECT COUNT(*) FROM "{table}"') or 0
            )
        except Exception:  # noqa: BLE001
            continue
    return counts


def _jobs_section(database: Database) -> dict[str, dict[str, int]]:
    return {
        "watch_jobs_by_state": _group_counts(database, "watch_jobs", "status"),
        "purchase_jobs_by_state": _group_counts(database, "purchase_jobs", "state"),
    }


def _group_counts(database: Database, table: str, column: str) -> dict[str, int]:
    try:
        rows = database.query_all(
            f'SELECT "{column}" AS bucket, COUNT(*) AS total FROM "{table}" '
            f'GROUP BY "{column}" ORDER BY bucket'
        )
    except Exception:  # noqa: BLE001
        return {}
    return {str(row["bucket"]): int(row["total"]) for row in rows}


def _settings_section(settings: SettingsService) -> dict[str, Any]:
    from dataclasses import asdict  # noqa: PLC0415 - only needed here

    values = asdict(settings.current)
    for key in REMOVED_SETTING_KEYS:
        values.pop(key, None)
    # StrEnum values are strings already; make that explicit so the JSON does
    # not depend on how the serialiser treats an enum.
    return {key: (str(value) if isinstance(value, str) else value) for key, value in values.items()}


def _capture_names(activity: ActivityRepository) -> list[dict[str, Any]]:
    """The saved captures, as file names and the step that produced them.

    The image itself is never read. The page is named under a key ending in
    ``url`` so that :func:`redact_mapping` runs it through
    :func:`app.diagnostics.redaction.redact_url` a second time, on top of the
    reduction the repository already applied when the row was written.
    """
    entries: list[dict[str, Any]] = []
    for row in activity.list_diagnostics():
        name = row.get("screenshot_file")
        entries.append(
            {
                "created_at": row.get("created_at"),
                "step": row.get("step"),
                "error_code": row.get("error_code"),
                "page_url": row.get("page_url"),
                "file": Path(str(name)).name if name else None,
            }
        )
    return entries


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def _is_excluded_path(path: Path, paths: AppPaths) -> bool:
    """True for anything inside the browser directory. See the module docstring."""
    try:
        resolved = path.resolve()
        browser_dir = paths.browser_dir.resolve()
    except OSError:  # pragma: no cover - defensive
        return True
    return resolved == browser_dir or browser_dir in resolved.parents


def _log_files(paths: AppPaths) -> list[Path]:
    """The live and rotated log files, oldest name first."""
    logs_dir = paths.logs_dir
    if not logs_dir.is_dir():
        return []
    candidates: list[Path] = []
    for pattern in (f"{TEXT_LOG_NAME}*", f"{EVENT_LOG_NAME}*"):
        for path in sorted(logs_dir.glob(pattern)):
            if path.is_file() and not _is_excluded_path(path, paths):
                candidates.append(path)
    return candidates


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _tail_lines(path: Path, limit: int) -> list[str]:
    """The last ``limit`` non-empty lines of a log, redacted again on the way out."""
    if limit <= 0 or not path.is_file():
        return []
    lines = [line for line in _read_text(path).splitlines() if line.strip()]
    return [redact_text(line) for line in lines[-limit:]]


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


def _render(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False, default=str)
