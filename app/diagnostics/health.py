"""Health checks, written for the person who has to read them.

The Settings -> Diagnostics screen and the startup sequence both ask the same
question: is anything about this installation going to stop it working? The
answer has to be usable by someone who will not read a log file, so every
check produces three things -- a short name, one plain sentence of detail, and
a concrete next step when something is wrong.

Design notes
------------

*Every dependency is optional.* A health report is most valuable exactly when
something failed to start. If the database could not be opened, the report
still has to say so and still has to report the disk space and the data
folder. Each dependency is therefore an optional constructor argument and its
checks are skipped rather than crashing the report.

*Two thresholds for disk space, not one.* Chromium needs roughly 450 MB, so
under 500 MB free the browser cannot be installed or updated at all: a real
problem. Under 2 GB it will work today but a Windows update or a growing log
directory will break it soon: worth a warning while there is still time.

*Warnings are for "not set up yet", problems are for "broken".* A fresh
install with no Amazon account connected is not faulty, so it warns. A
missing browser is a thing the application cannot do its job without, so it
is a problem.

*Test mode always produces a check.* Test mode on means no order can be
placed, which is safe but worth stating plainly so nobody waits for a
purchase that will never happen. Test mode off means real money can be spent,
which the user should be reminded of in the one place they go to ask "is
everything all right?".
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

from app.branding import BRAND
from app.diagnostics.logger import get_logger
from app.winint import startup

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.automation.browser_manager import BrowserManager
    from app.config import SettingsService
    from app.database.database import Database
    from app.notifications.notifier import Notifier
    from app.paths import AppPaths

logger = get_logger("diagnostics.health")

#: Below this much free space the browser cannot be installed or updated.
DISK_PROBLEM_BYTES: Final = 500 * 1024 * 1024

#: Below this much free space everything still works, but not for long.
DISK_WARNING_BYTES: Final = 2 * 1024 * 1024 * 1024

#: Roughly what a Chromium build occupies, quoted in the fix hints.
BROWSER_SIZE_TEXT: Final = "about 450 MB"

#: Check names. Module level so the UI and the tests can refer to one of them
#: without re-typing the string.
CHECK_DATA_FOLDER: Final = "Data folder"
CHECK_DATABASE: Final = "Saved data"
CHECK_DISK_SPACE: Final = "Free disk space"
CHECK_BROWSER: Final = "Browser"
CHECK_ACCOUNT: Final = "Amazon account"
CHECK_NOTIFICATIONS: Final = "Notifications"
CHECK_STARTUP: Final = "Start with Windows"
CHECK_TEST_MODE: Final = "Test mode"


class CheckOutcome(StrEnum):
    """How a single check turned out."""

    OK = "ok"
    WARNING = "warning"
    PROBLEM = "problem"

    @property
    def label(self) -> str:
        """Short wording for the diagnostics list."""
        return {
            CheckOutcome.OK: "Fine",
            CheckOutcome.WARNING: "Worth a look",
            CheckOutcome.PROBLEM: "Needs attention",
        }[self]


@dataclass(frozen=True)
class HealthCheck:
    """The result of one check, ready to render as a row."""

    name: str
    outcome: CheckOutcome
    detail: str
    #: What the user can do about it. ``None`` when there is nothing to do.
    fix_hint: str | None = None


@dataclass(frozen=True)
class HealthReport:
    """Every check from one run, in the order they were performed."""

    checks: tuple[HealthCheck, ...]

    @property
    def healthy(self) -> bool:
        """True when nothing is broken. Warnings do not make it false."""
        return not self.problems

    @property
    def problems(self) -> tuple[HealthCheck, ...]:
        return tuple(
            check for check in self.checks if check.outcome is CheckOutcome.PROBLEM
        )

    @property
    def warnings(self) -> tuple[HealthCheck, ...]:
        return tuple(
            check for check in self.checks if check.outcome is CheckOutcome.WARNING
        )

    @property
    def summary(self) -> str:
        """One line for the top of the diagnostics screen."""
        problems = len(self.problems)
        warnings = len(self.warnings)
        if problems == 1:
            return "1 problem needs attention"
        if problems > 1:
            return f"{problems} problems need attention"
        if warnings == 1:
            return "1 thing is worth a look"
        if warnings > 1:
            return f"{warnings} things are worth a look"
        if not self.checks:
            return "Nothing could be checked"
        return "Everything looks fine"

    def to_dict(self) -> dict[str, object]:
        """A plain structure for the exported diagnostic report."""
        return {
            "healthy": self.healthy,
            "summary": self.summary,
            "checks": [
                {
                    "name": check.name,
                    "outcome": check.outcome.value,
                    "detail": check.detail,
                    "fix_hint": check.fix_hint,
                }
                for check in self.checks
            ],
        }


def _probe_write(root: Path) -> None:
    """Prove the data folder is writable by writing and deleting a file.

    A separate function because asking the filesystem is the only honest test
    -- a read-only folder, a full disk and a folder the antivirus has locked
    all look identical until something is actually written -- and because it
    is the one part of the health check a test needs to make fail.
    """
    root.mkdir(parents=True, exist_ok=True)
    probe = root / f"write-test-{os.getpid()}.tmp"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()


class HealthService:
    """Runs every check that its available dependencies allow."""

    def __init__(
        self,
        *,
        paths: AppPaths | None = None,
        database: Database | None = None,
        settings: SettingsService | None = None,
        browser_manager: BrowserManager | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self._paths = paths
        self._database = database
        self._settings = settings
        self._browser = browser_manager
        self._notifier = notifier

    def run(self) -> HealthReport:
        """Perform every possible check and return the report."""
        checks: list[HealthCheck] = []
        for check in (
            self._check_data_folder(),
            self._check_database(),
            self._check_disk_space(),
            self._check_browser(),
            self._check_account(),
            self._check_notifications(),
            self._check_startup(),
            self._check_test_mode(),
        ):
            if check is not None:
                checks.append(check)

        report = HealthReport(checks=tuple(checks))
        logger.info(
            "Health check finished",
            extra={
                "summary": report.summary,
                "problems": len(report.problems),
                "warnings": len(report.warnings),
            },
        )
        return report

    # ---- individual checks ----------------------------------------------

    def _check_data_folder(self) -> HealthCheck | None:
        if self._paths is None:
            return None
        root = self._paths.root
        try:
            _probe_write(root)
        except OSError as exc:
            logger.warning("The data folder is not writable: %s", exc)
            return HealthCheck(
                name=CHECK_DATA_FOLDER,
                outcome=CheckOutcome.PROBLEM,
                detail=f"The app cannot save anything to {root}.",
                fix_hint=(
                    "Check that the folder still exists and that your Windows "
                    "account is allowed to write to it, then try again. If a "
                    "backup or antivirus tool is scanning the folder, wait for "
                    "it to finish."
                ),
            )
        return HealthCheck(
            name=CHECK_DATA_FOLDER,
            outcome=CheckOutcome.OK,
            detail=f"The app can save its data in {root}.",
        )

    def _check_database(self) -> HealthCheck | None:
        if self._database is None:
            return None
        try:
            intact, result = self._database.integrity_check()
        except Exception as exc:  # noqa: BLE001 - the report must still be produced
            logger.warning("Could not check the saved data: %s", exc)
            return HealthCheck(
                name=CHECK_DATABASE,
                outcome=CheckOutcome.PROBLEM,
                detail="The app could not open its saved data.",
                fix_hint=(
                    "Restart the app. If it still cannot open your data, "
                    "restore the most recent backup from this screen."
                ),
            )
        if not intact:
            return HealthCheck(
                name=CHECK_DATABASE,
                outcome=CheckOutcome.PROBLEM,
                detail=f"The file holding your saved data reports a fault ({result}).",
                fix_hint=(
                    "Restore the most recent backup from this screen. Your "
                    "watched products and rules are in the backup."
                ),
            )

        current = self._database.current_version()
        target = self._database.target_version()
        if current > target:
            return HealthCheck(
                name=CHECK_DATABASE,
                outcome=CheckOutcome.PROBLEM,
                detail=(
                    "Your saved data was written by a newer version of the app "
                    "than the one running."
                ),
                fix_hint=f"Install the latest version of {BRAND.display_name}.",
            )
        if current < target:
            return HealthCheck(
                name=CHECK_DATABASE,
                outcome=CheckOutcome.WARNING,
                detail="Your saved data has not finished being updated.",
                fix_hint="Restart the app to finish updating it.",
            )
        return HealthCheck(
            name=CHECK_DATABASE,
            outcome=CheckOutcome.OK,
            detail="Your saved data is readable and up to date.",
        )

    def _check_disk_space(self) -> HealthCheck | None:
        if self._paths is None:
            return None
        try:
            usage = shutil.disk_usage(self._paths.root)
        except OSError as exc:
            logger.warning("Could not read the free disk space: %s", exc)
            return HealthCheck(
                name=CHECK_DISK_SPACE,
                outcome=CheckOutcome.WARNING,
                detail="The app could not find out how much disk space is free.",
                fix_hint="Check that the drive holding your data is still connected.",
            )

        free = int(usage.free)
        readable = format_size(free)
        if free < DISK_PROBLEM_BYTES:
            return HealthCheck(
                name=CHECK_DISK_SPACE,
                outcome=CheckOutcome.PROBLEM,
                detail=(
                    f"Only {readable} is free. The browser the app uses needs "
                    f"{BROWSER_SIZE_TEXT} on its own."
                ),
                fix_hint="Free up some space on this drive, then run these checks again.",
            )
        if free < DISK_WARNING_BYTES:
            return HealthCheck(
                name=CHECK_DISK_SPACE,
                outcome=CheckOutcome.WARNING,
                detail=(
                    f"{readable} is free. That is enough for now, but the app "
                    "will run out of room before long."
                ),
                fix_hint="Free up some space on this drive when you get the chance.",
            )
        return HealthCheck(
            name=CHECK_DISK_SPACE,
            outcome=CheckOutcome.OK,
            detail=f"{readable} is free on this drive.",
        )

    def _check_browser(self) -> HealthCheck | None:
        if self._browser is None:
            return None
        try:
            info = self._browser.info()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not inspect the browser: %s", exc)
            return HealthCheck(
                name=CHECK_BROWSER,
                outcome=CheckOutcome.PROBLEM,
                detail="The app could not find out whether its browser is installed.",
                fix_hint="Open Settings and install the browser again.",
            )
        if not info.installed:
            return HealthCheck(
                name=CHECK_BROWSER,
                outcome=CheckOutcome.PROBLEM,
                detail=(
                    "The browser the app uses to visit Amazon has not been "
                    "installed yet, so nothing can be checked or bought."
                ),
                fix_hint=(
                    "Open Settings and choose Install browser. It downloads "
                    f"{BROWSER_SIZE_TEXT} once."
                ),
            )
        return HealthCheck(
            name=CHECK_BROWSER,
            outcome=CheckOutcome.OK,
            detail=f"The browser is installed ({info.summary}).",
        )

    def _check_account(self) -> HealthCheck | None:
        if self._settings is None:
            return None
        current = self._settings.current
        if not current.amazon_connected:
            # Not a problem: a new installation is expected to look like this
            # until the user signs in once.
            return HealthCheck(
                name=CHECK_ACCOUNT,
                outcome=CheckOutcome.WARNING,
                detail=(
                    "No Amazon account is connected yet, so nothing can be "
                    "watched or bought."
                ),
                fix_hint="Open Settings and connect your Amazon account.",
            )
        return HealthCheck(
            name=CHECK_ACCOUNT,
            outcome=CheckOutcome.OK,
            detail=f"Connected to Amazon on {current.marketplace}.",
        )

    def _check_notifications(self) -> HealthCheck | None:
        if self._notifier is None:
            return None
        from app.notifications.notifier import (  # noqa: PLC0415 - avoids a cycle
            CHANNEL_TOAST,
            CHANNEL_TRAY,
        )

        channel = self._notifier.available_channel()
        if channel == CHANNEL_TOAST:
            return HealthCheck(
                name=CHECK_NOTIFICATIONS,
                outcome=CheckOutcome.OK,
                detail="Windows notifications are available.",
            )
        if channel == CHANNEL_TRAY:
            return HealthCheck(
                name=CHECK_NOTIFICATIONS,
                outcome=CheckOutcome.WARNING,
                detail=(
                    "Windows notifications are not available, so messages "
                    "appear next to the clock instead."
                ),
                fix_hint=(
                    "Send a test notification from this screen. If nothing "
                    f"appears, allow notifications for {BRAND.display_name} in "
                    "Windows Settings."
                ),
            )
        return HealthCheck(
            name=CHECK_NOTIFICATIONS,
            outcome=CheckOutcome.PROBLEM,
            detail=(
                "There is no way to tell you when something happens, so you "
                "will only see it inside the app."
            ),
            fix_hint=(
                f"Allow notifications for {BRAND.display_name} in Windows "
                "Settings, then send a test notification from this screen."
            ),
        )

    def _check_startup(self) -> HealthCheck | None:
        try:
            status = startup.status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read the start with Windows setting: %s", exc)
            return HealthCheck(
                name=CHECK_STARTUP,
                outcome=CheckOutcome.WARNING,
                detail="The app could not find out whether it starts with Windows.",
                fix_hint="Turn start with Windows off and on again in Settings.",
            )

        if status.disabled_by_windows:
            # The registry entry is there, but the user's own switch in
            # Windows Settings overrides it. Saying "on" here would
            # contradict Windows and the app would never start.
            return HealthCheck(
                name=CHECK_STARTUP,
                outcome=CheckOutcome.WARNING,
                detail=(
                    "The app is set to start with Windows, but Windows "
                    "Settings has it switched off, so it will not start."
                ),
                fix_hint=(
                    "Open Startup apps in Windows Settings and switch "
                    f"{BRAND.display_name} back on."
                ),
            )

        wanted = self._settings.current.start_with_windows if self._settings else None
        if wanted and not status.registered:
            return HealthCheck(
                name=CHECK_STARTUP,
                outcome=CheckOutcome.WARNING,
                detail=(
                    "The app is meant to start with Windows, but the entry "
                    "that does it is missing."
                ),
                fix_hint="Turn start with Windows off and on again in Settings.",
            )
        if status.effective:
            return HealthCheck(
                name=CHECK_STARTUP,
                outcome=CheckOutcome.OK,
                detail="The app starts with Windows and keeps watching in the background.",
            )
        return HealthCheck(
            name=CHECK_STARTUP,
            outcome=CheckOutcome.OK,
            detail="The app does not start with Windows. You open it yourself.",
        )

    def _check_test_mode(self) -> HealthCheck | None:
        if self._settings is None:
            return None
        if self._settings.current.test_mode:
            return HealthCheck(
                name=CHECK_TEST_MODE,
                outcome=CheckOutcome.OK,
                detail=(
                    "Test mode is on. The app goes all the way to the last "
                    "step and then stops, so no order can be placed."
                ),
                fix_hint=(
                    "Turn test mode off in Settings when you want the app to "
                    "buy for real."
                ),
            )
        return HealthCheck(
            name=CHECK_TEST_MODE,
            outcome=CheckOutcome.WARNING,
            detail=(
                "Test mode is off, so real orders are possible and real money "
                "can be spent on your Amazon account."
            ),
            fix_hint="Turn test mode on in Settings if you are only trying things out.",
        )


def format_size(num_bytes: int) -> str:
    """A size a person can read, e.g. ``1.4 GB``. Used in the check details."""
    if num_bytes < 1024:
        return f"{int(num_bytes)} bytes"
    for unit, scale in (
        ("GB", 1024**3),
        ("MB", 1024**2),
        ("KB", 1024),
    ):
        if num_bytes >= scale:
            value = num_bytes / scale
            rendered = f"{value:.1f}".rstrip("0").rstrip(".")
            return f"{rendered} {unit}"
    return f"{int(num_bytes)} bytes"  # pragma: no cover - unreachable
