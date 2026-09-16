"""Tests for :mod:`app.diagnostics.health`.

``shutil.disk_usage`` and :func:`app.winint.startup.status` are replaced in
almost every test: both answer questions about the machine the suite happens
to be running on, and a health report has to be deterministic. The startup
stand-in also keeps the tests away from the real ``Run`` registry key.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Any

import pytest

from app.automation.browser_manager import BrowserInfo
from app.config import SettingsService
from app.database.database import Database
from app.diagnostics import health as health_module
from app.diagnostics.health import (
    CHECK_ACCOUNT,
    CHECK_BROWSER,
    CHECK_DATA_FOLDER,
    CHECK_DATABASE,
    CHECK_DISK_SPACE,
    CHECK_NOTIFICATIONS,
    CHECK_STARTUP,
    CHECK_TEST_MODE,
    CheckOutcome,
    HealthCheck,
    HealthReport,
    HealthService,
)
from app.notifications.notifier import CHANNEL_NONE, CHANNEL_TOAST, CHANNEL_TRAY
from app.paths import AppPaths
from app.winint import startup

PLENTY_OF_SPACE = 200 * 1024**3
LOW_SPACE = 1024**3
ALMOST_NO_SPACE = 100 * 1024**2


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeUsage:
    total: int
    used: int
    free: int


class FakeBrowserManager:
    def __init__(self, *, installed: bool = True) -> None:
        self._installed = installed

    def info(self) -> BrowserInfo:
        return BrowserInfo(
            installed=self._installed,
            executable_path="C:/data/browser/chrome.exe" if self._installed else None,
            version="Chromium build 1243" if self._installed else None,
            profile_dir="C:/data/browser/amazon-profile",
            browsers_dir="C:/data/browser/playwright",
            playwright_version="1.63.0",
            running=False,
        )

    def profile_size_bytes(self) -> int:
        return 4096


class FakeNotifier:
    def __init__(self, channel: str = CHANNEL_TOAST) -> None:
        self._channel = channel

    def available_channel(self) -> str:
        return self._channel


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _predictable_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixed free space and a fixed autostart answer, off the real registry."""
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: FakeUsage(
            total=PLENTY_OF_SPACE * 2, used=PLENTY_OF_SPACE, free=PLENTY_OF_SPACE
        ),
    )
    monkeypatch.setattr(
        startup,
        "status",
        lambda: startup.StartupStatus(
            registered=False, disabled_by_windows=False, command=None, effective=False
        ),
    )


@pytest.fixture
def settings(database: Database) -> SettingsService:
    return SettingsService(database)


@pytest.fixture
def connected(settings: SettingsService) -> SettingsService:
    """Settings that look like a finished, cautious setup."""
    settings.update(amazon_connected=True, test_mode=True)
    return settings


@pytest.fixture
def service(
    app_paths: AppPaths, database: Database, connected: SettingsService
) -> HealthService:
    return HealthService(
        paths=app_paths,
        database=database,
        settings=connected,
        browser_manager=FakeBrowserManager(),
        notifier=FakeNotifier(),
    )


def find(report: HealthReport, name: str) -> HealthCheck:
    matches = [check for check in report.checks if check.name == name]
    assert matches, f"no check named {name!r} in {[c.name for c in report.checks]}"
    return matches[0]


def set_free_space(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: FakeUsage(total=free * 4, used=free * 3, free=free),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHealthySetup:
    def test_a_finished_setup_is_healthy(self, service: HealthService) -> None:
        report = service.run()

        assert report.healthy is True
        assert report.problems == ()
        assert report.warnings == ()
        assert report.summary == "Everything looks fine"

    def test_every_check_is_present(self, service: HealthService) -> None:
        names = [check.name for check in service.run().checks]
        assert names == [
            CHECK_DATA_FOLDER,
            CHECK_DATABASE,
            CHECK_DISK_SPACE,
            CHECK_BROWSER,
            CHECK_ACCOUNT,
            CHECK_NOTIFICATIONS,
            CHECK_STARTUP,
            CHECK_TEST_MODE,
        ]

    def test_a_fresh_install_warns_rather_than_failing(
        self,
        app_paths: AppPaths,
        database: Database,
        settings: SettingsService,
    ) -> None:
        """Nothing connected yet is incomplete, not broken."""
        report = HealthService(
            paths=app_paths,
            database=database,
            settings=settings,
            browser_manager=FakeBrowserManager(),
            notifier=FakeNotifier(),
        ).run()

        assert report.healthy is True
        assert find(report, CHECK_ACCOUNT).outcome is CheckOutcome.WARNING
        assert find(report, CHECK_ACCOUNT).fix_hint


class TestDataFolder:
    def test_a_folder_it_cannot_write_to_is_a_problem(
        self, service: HealthService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _refuse(_root: Any) -> None:
            raise PermissionError("Access is denied")

        monkeypatch.setattr(health_module, "_probe_write", _refuse)

        report = service.run()
        check = find(report, CHECK_DATA_FOLDER)

        assert report.healthy is False
        assert check.outcome is CheckOutcome.PROBLEM
        assert check.fix_hint
        assert "cannot save" in check.detail

    def test_a_missing_folder_is_a_problem(
        self, service: HealthService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _gone(_root: Any) -> None:
            raise FileNotFoundError("The directory name is invalid")

        monkeypatch.setattr(health_module, "_probe_write", _gone)

        assert find(service.run(), CHECK_DATA_FOLDER).outcome is CheckOutcome.PROBLEM

    def test_a_writable_folder_leaves_nothing_behind(
        self, service: HealthService, app_paths: AppPaths
    ) -> None:
        service.run()

        assert list(app_paths.root.glob("write-test-*.tmp")) == []


class TestDiskSpace:
    def test_plenty_of_space_is_fine(self, service: HealthService) -> None:
        assert find(service.run(), CHECK_DISK_SPACE).outcome is CheckOutcome.OK

    def test_running_low_is_a_warning(
        self, service: HealthService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_free_space(monkeypatch, LOW_SPACE)

        report = service.run()
        check = find(report, CHECK_DISK_SPACE)

        assert check.outcome is CheckOutcome.WARNING
        assert report.healthy is True
        assert check.fix_hint

    def test_almost_none_is_a_problem(
        self, service: HealthService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_free_space(monkeypatch, ALMOST_NO_SPACE)

        report = service.run()
        check = find(report, CHECK_DISK_SPACE)

        assert check.outcome is CheckOutcome.PROBLEM
        assert report.healthy is False
        assert "450 MB" in check.detail

    def test_a_drive_it_cannot_read_is_a_warning(
        self, service: HealthService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(_path: Any) -> FakeUsage:
            raise OSError("The device is not ready")

        monkeypatch.setattr(shutil, "disk_usage", _fail)

        assert find(service.run(), CHECK_DISK_SPACE).outcome is CheckOutcome.WARNING


class TestBrowser:
    def test_a_missing_browser_is_a_problem_with_a_way_out(
        self,
        app_paths: AppPaths,
        database: Database,
        connected: SettingsService,
    ) -> None:
        report = HealthService(
            paths=app_paths,
            database=database,
            settings=connected,
            browser_manager=FakeBrowserManager(installed=False),
            notifier=FakeNotifier(),
        ).run()
        check = find(report, CHECK_BROWSER)

        assert report.healthy is False
        assert check.outcome is CheckOutcome.PROBLEM
        assert check.fix_hint is not None
        assert "Install browser" in check.fix_hint

    def test_an_installed_browser_reports_its_version(
        self, service: HealthService
    ) -> None:
        check = find(service.run(), CHECK_BROWSER)

        assert check.outcome is CheckOutcome.OK
        assert "Chromium build 1243" in check.detail


class TestDatabase:
    def test_a_migrated_database_is_fine(self, service: HealthService) -> None:
        assert find(service.run(), CHECK_DATABASE).outcome is CheckOutcome.OK

    def test_a_damaged_database_is_a_problem(
        self, service: HealthService, database: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            database, "integrity_check", lambda: (False, "database disk image is malformed")
        )

        check = find(service.run(), CHECK_DATABASE)

        assert check.outcome is CheckOutcome.PROBLEM
        assert check.fix_hint

    def test_data_from_a_newer_version_is_a_problem(
        self, service: HealthService, database: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(database, "current_version", lambda: 99)

        check = find(service.run(), CHECK_DATABASE)

        assert check.outcome is CheckOutcome.PROBLEM
        assert "newer version" in check.detail

    def test_an_unfinished_update_is_a_warning(
        self, service: HealthService, database: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(database, "target_version", lambda: 99)

        assert find(service.run(), CHECK_DATABASE).outcome is CheckOutcome.WARNING


class TestNotifications:
    def test_working_toasts_are_fine(self, service: HealthService) -> None:
        assert find(service.run(), CHECK_NOTIFICATIONS).outcome is CheckOutcome.OK

    @pytest.mark.parametrize(
        ("channel", "outcome"),
        [
            (CHANNEL_TRAY, CheckOutcome.WARNING),
            (CHANNEL_NONE, CheckOutcome.PROBLEM),
        ],
    )
    def test_a_weaker_channel_is_reported(
        self,
        channel: str,
        outcome: CheckOutcome,
        app_paths: AppPaths,
        database: Database,
        connected: SettingsService,
    ) -> None:
        report = HealthService(
            paths=app_paths,
            database=database,
            settings=connected,
            browser_manager=FakeBrowserManager(),
            notifier=FakeNotifier(channel),
        ).run()
        check = find(report, CHECK_NOTIFICATIONS)

        assert check.outcome is outcome
        assert check.fix_hint


class TestStartWithWindows:
    def test_an_entry_windows_has_switched_off_is_called_out(
        self, service: HealthService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            startup,
            "status",
            lambda: startup.StartupStatus(
                registered=True,
                disabled_by_windows=True,
                command='"app.exe" --tray',
                effective=False,
            ),
        )

        check = find(service.run(), CHECK_STARTUP)

        assert check.outcome is CheckOutcome.WARNING
        assert "Windows Settings" in check.detail
        assert check.fix_hint is not None
        assert "Startup apps" in check.fix_hint

    def test_a_working_entry_is_fine(
        self, service: HealthService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            startup,
            "status",
            lambda: startup.StartupStatus(
                registered=True,
                disabled_by_windows=False,
                command='"app.exe" --tray',
                effective=True,
            ),
        )

        check = find(service.run(), CHECK_STARTUP)

        assert check.outcome is CheckOutcome.OK
        assert "starts with Windows" in check.detail

    def test_not_starting_with_windows_is_fine(self, service: HealthService) -> None:
        assert find(service.run(), CHECK_STARTUP).outcome is CheckOutcome.OK

    def test_a_missing_entry_the_user_asked_for_is_a_warning(
        self, service: HealthService, connected: SettingsService
    ) -> None:
        connected.update(start_with_windows=True)

        check = find(service.run(), CHECK_STARTUP)

        assert check.outcome is CheckOutcome.WARNING
        assert check.fix_hint


class TestTestMode:
    def test_test_mode_on_is_reported_as_safe(self, service: HealthService) -> None:
        check = find(service.run(), CHECK_TEST_MODE)

        assert check.outcome is CheckOutcome.OK
        assert "no order can be placed" in check.detail

    def test_test_mode_off_says_real_orders_are_possible(
        self, service: HealthService, connected: SettingsService
    ) -> None:
        connected.update(test_mode=False)

        check = find(service.run(), CHECK_TEST_MODE)

        assert check.outcome is CheckOutcome.WARNING
        assert "real orders are possible" in check.detail
        assert check.fix_hint


class TestPartialReports:
    def test_a_report_is_possible_with_nothing_available(self) -> None:
        report = HealthService().run()

        # Only the autostart check needs no dependency at all.
        assert [check.name for check in report.checks] == [CHECK_STARTUP]
        assert report.healthy is True

    def test_only_a_data_folder_still_reports_disk_space(
        self, app_paths: AppPaths
    ) -> None:
        names = [check.name for check in HealthService(paths=app_paths).run().checks]

        assert CHECK_DATA_FOLDER in names
        assert CHECK_DISK_SPACE in names
        assert CHECK_DATABASE not in names


class TestWording:
    @pytest.mark.parametrize("outcome", list(CheckOutcome))
    def test_every_outcome_has_a_plain_label(self, outcome: CheckOutcome) -> None:
        assert outcome.label
        assert "_" not in outcome.label
        assert outcome.label != outcome.value

    def test_summaries_read_as_plain_english(
        self, service: HealthService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        summaries = [service.run().summary]

        set_free_space(monkeypatch, LOW_SPACE)
        summaries.append(service.run().summary)

        set_free_space(monkeypatch, ALMOST_NO_SPACE)
        summaries.append(service.run().summary)

        assert summaries == [
            "Everything looks fine",
            "1 thing is worth a look",
            "1 problem needs attention",
        ]
        for summary in summaries:
            assert "_" not in summary

    def test_several_problems_are_counted(self) -> None:
        report = HealthReport(
            checks=(
                HealthCheck("A", CheckOutcome.PROBLEM, "broken"),
                HealthCheck("B", CheckOutcome.PROBLEM, "also broken"),
                HealthCheck("C", CheckOutcome.WARNING, "hmm"),
            )
        )

        assert report.summary == "2 problems need attention"
        assert report.healthy is False
        assert len(report.warnings) == 1

    def test_several_warnings_are_counted(self) -> None:
        report = HealthReport(
            checks=(
                HealthCheck("A", CheckOutcome.WARNING, "hmm"),
                HealthCheck("B", CheckOutcome.WARNING, "also hmm"),
            )
        )

        assert report.summary == "2 things are worth a look"

    def test_no_check_detail_mentions_an_internal_name(
        self, service: HealthService
    ) -> None:
        for check in service.run().checks:
            if check.name == CHECK_DATA_FOLDER:
                continue  # quotes a real path, which may contain anything
            assert "_" not in check.detail
            assert "_" not in (check.fix_hint or "")

    def test_an_empty_report_says_so(self) -> None:
        assert HealthReport(checks=()).summary == "Nothing could be checked"


class TestReportShape:
    def test_it_converts_to_a_plain_structure(self, service: HealthService) -> None:
        data = service.run().to_dict()

        assert data["healthy"] is True
        assert data["summary"]
        assert isinstance(data["checks"], list)
        assert {"name", "outcome", "detail", "fix_hint"} == set(data["checks"][0])

    def test_format_size_is_readable(self) -> None:
        assert health_module.format_size(512) == "512 bytes"
        assert health_module.format_size(2 * 1024) == "2 KB"
        assert health_module.format_size(1536 * 1024) == "1.5 MB"
        assert health_module.format_size(3 * 1024**3) == "3 GB"
