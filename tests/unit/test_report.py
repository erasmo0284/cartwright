"""Tests for :mod:`app.diagnostics.report`.

``TestNothingSensitiveEscapes`` is the reason this file exists. It plants
real-shaped secrets everywhere the report gathers from -- the settings table,
a diagnostics row's metadata and web address, and the event log -- and then
searches the serialised report for each literal. A bundle a user is asked to
email to support has to be provably free of anything that would let someone
else use their Amazon account.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.config import SettingsService
from app.database.database import Database
from app.database.repositories.activity import ActivityRepository
from app.diagnostics import report as report_module
from app.diagnostics.health import CheckOutcome, HealthCheck, HealthReport
from app.diagnostics.logger import EVENT_LOG_NAME, TEXT_LOG_NAME
from app.diagnostics.redaction import REDACTED
from app.diagnostics.report import (
    build_report,
    clear_diagnostics,
    export_zip,
    write_report,
)
from app.paths import AppPaths
from app.winint import startup

from tests.unit.test_health import FakeBrowserManager, FakeNotifier

# Secrets planted in the sources the report reads from. Each one is searched
# for, as a literal, in the serialised report.
COOKIE_VALUE = "SEKRET0COOKIE0VALUE"
TOKEN_VALUE = "SEKRET1TOKEN1VALUE"
PASSWORD_VALUE = "SEKRET2PASSWORD2VALUE"
CARD_NUMBER = "4111111111111111"
ACCOUNT_LABEL = "Jane Q Buyer"
QUERY_SECRET = "SEKRET3QUERY3VALUE"

ALL_SECRETS = (
    COOKIE_VALUE,
    TOKEN_VALUE,
    PASSWORD_VALUE,
    CARD_NUMBER,
    ACCOUNT_LABEL,
    QUERY_SECRET,
)


@pytest.fixture(autouse=True)
def _off_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the autostart health check away from the real registry."""
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
def activity(database: Database) -> ActivityRepository:
    return ActivityRepository(database)


@pytest.fixture
def deps(
    app_paths: AppPaths,
    database: Database,
    settings: SettingsService,
    activity: ActivityRepository,
) -> dict[str, object]:
    """Everything :func:`build_report` can use, as keyword arguments."""
    return {
        "paths": app_paths,
        "database": database,
        "settings": settings,
        "activity": activity,
        "browser_manager": FakeBrowserManager(),
        "notifier": FakeNotifier(),
    }


def write_event_log(app_paths: AppPaths, *lines: str) -> Path:
    path = app_paths.logs_dir / EVENT_LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def plant_secrets(
    app_paths: AppPaths, settings: SettingsService, activity: ActivityRepository
) -> None:
    """Put a secret into every source the report gathers from."""
    settings.update(
        amazon_connected=True,
        amazon_account_label=ACCOUNT_LABEL,
    )
    activity.record_diagnostic(
        step="checkout",
        error_code="checkout_changed",
        page_url=(
            "https://www.amazon.com/gp/buy/spc/handlers/display.html"
            f"?session-id={QUERY_SECRET}&ref=nav"
        ),
        screenshot_file="checkout-2026-09-16.png",
        metadata={
            "cookie": f"session-id={COOKIE_VALUE}",
            "password": PASSWORD_VALUE,
            "card_number": CARD_NUMBER,
        },
    )
    write_event_log(
        app_paths,
        json.dumps(
            {
                "ts": "2026-09-16T10:00:00",
                "level": "INFO",
                "message": "loaded the checkout page",
                "cookie": f"session-id={COOKIE_VALUE}",
            }
        ),
        json.dumps(
            {
                "ts": "2026-09-16T10:00:01",
                "level": "WARNING",
                "message": "retrying",
                "session-token": TOKEN_VALUE,
                "password": PASSWORD_VALUE,
            }
        ),
        json.dumps(
            {
                "ts": "2026-09-16T10:00:02",
                "level": "ERROR",
                "message": f"card {CARD_NUMBER} was declined",
            }
        ),
    )


class TestShape:
    def test_the_report_has_the_expected_sections(
        self, deps: dict[str, object]
    ) -> None:
        report = build_report(**deps)

        assert {
            "generated_at",
            "app",
            "system",
            "health",
            "folders",
            "recent_log",
            "browser",
            "database",
            "jobs",
            "settings",
            "diagnostic_captures",
            "_excluded",
        } <= set(report)

    def test_it_identifies_the_build(self, deps: dict[str, object]) -> None:
        app = build_report(**deps)["app"]

        assert app["version"]
        assert app["build_channel"] in {"release", "dev"}
        assert app["frozen"] is False

    def test_it_reports_the_operating_system_and_python(
        self, deps: dict[str, object]
    ) -> None:
        system = build_report(**deps)["system"]

        assert system["os"]
        assert system["python"].startswith("3.")

    def test_it_carries_the_health_report(self, deps: dict[str, object]) -> None:
        health = build_report(**deps)["health"]

        assert "summary" in health
        assert health["checks"]

    def test_a_prepared_health_report_is_used_as_given(
        self, deps: dict[str, object]
    ) -> None:
        prepared = HealthReport(
            checks=(HealthCheck("Made up", CheckOutcome.PROBLEM, "for the test"),)
        )

        report = build_report(health=prepared, **deps)

        assert report["health"]["summary"] == "1 problem needs attention"

    def test_it_counts_the_rows_in_every_table(
        self, deps: dict[str, object], settings: SettingsService
    ) -> None:
        settings.update(monitoring_enabled=False)

        counts = build_report(**deps)["database"]["row_counts"]

        assert counts["settings"] == 1
        assert counts["watch_jobs"] == 0
        assert "purchase_jobs" in counts

    def test_it_reports_the_schema_version(self, deps: dict[str, object]) -> None:
        database = build_report(**deps)["database"]

        assert database["schema_version"] == database["schema_target"]
        assert database["integrity_ok"] is True
        assert database["size_bytes"] > 0

    def test_it_groups_the_jobs_by_state(
        self, deps: dict[str, object], database: Database
    ) -> None:
        jobs = build_report(**deps)["jobs"]

        assert jobs["watch_jobs_by_state"] == {}
        assert jobs["purchase_jobs_by_state"] == {}

    def test_it_lists_the_browser(self, deps: dict[str, object]) -> None:
        browser = build_report(**deps)["browser"]

        assert browser["installed"] is True
        assert browser["playwright_version"] == "1.63.0"

    def test_a_report_is_possible_with_nothing_available(self) -> None:
        report = build_report()

        assert report["_excluded"]
        assert "database" not in report

    def test_it_reads_the_last_log_lines(
        self, deps: dict[str, object], app_paths: AppPaths
    ) -> None:
        write_event_log(app_paths, *[f'{{"n": {index}}}' for index in range(500)])

        recent = build_report(log_lines=25, **deps)["recent_log"]

        assert len(recent) == 25
        assert recent[-1] == '{"n": 499}'


class TestNothingSensitiveEscapes:
    def test_no_planted_secret_survives(
        self,
        deps: dict[str, object],
        app_paths: AppPaths,
        settings: SettingsService,
        activity: ActivityRepository,
    ) -> None:
        plant_secrets(app_paths, settings, activity)

        serialised = json.dumps(build_report(**deps))

        for secret in ALL_SECRETS:
            assert secret not in serialised, f"{secret} leaked into the report"

    def test_the_account_label_is_removed_from_the_settings(
        self,
        deps: dict[str, object],
        app_paths: AppPaths,
        settings: SettingsService,
        activity: ActivityRepository,
    ) -> None:
        plant_secrets(app_paths, settings, activity)

        report = build_report(**deps)

        assert "amazon_account_label" not in report["settings"]
        assert report["settings"]["amazon_connected"] is True

    def test_the_log_lines_are_redacted_a_second_time(
        self,
        deps: dict[str, object],
        app_paths: AppPaths,
        settings: SettingsService,
        activity: ActivityRepository,
    ) -> None:
        """The logger redacts on the way in; the report does not rely on it."""
        plant_secrets(app_paths, settings, activity)

        recent = build_report(**deps)["recent_log"]

        assert recent
        assert any(REDACTED in line for line in recent)

    def test_a_web_address_keeps_the_page_and_drops_the_query(
        self,
        deps: dict[str, object],
        app_paths: AppPaths,
        settings: SettingsService,
        activity: ActivityRepository,
    ) -> None:
        plant_secrets(app_paths, settings, activity)

        captures = build_report(**deps)["diagnostic_captures"]

        assert captures[0]["page_url"].endswith(f"?{REDACTED}")
        assert "/gp/buy/spc/" in captures[0]["page_url"]

    def test_only_the_screenshot_file_name_is_listed(
        self,
        deps: dict[str, object],
        app_paths: AppPaths,
        settings: SettingsService,
        activity: ActivityRepository,
    ) -> None:
        plant_secrets(app_paths, settings, activity)

        captures = build_report(**deps)["diagnostic_captures"]

        assert captures[0]["file"] == "checkout-2026-09-16.png"
        assert captures[0]["step"] == "checkout"

    def test_the_excluded_list_says_what_is_missing(
        self, deps: dict[str, object]
    ) -> None:
        excluded = build_report(**deps)["_excluded"]

        assert excluded
        assert all(entry["item"] and entry["why"] for entry in excluded)
        joined = " ".join(entry["item"] for entry in excluded).lower()
        assert "browser profile" in joined
        assert "screenshots" in joined


class TestWriteReport:
    def test_it_writes_json_that_round_trips(
        self, deps: dict[str, object], tmp_path: Path
    ) -> None:
        destination = tmp_path / "out" / "diagnostics.json"

        written = write_report(destination, **deps)

        assert written == destination
        loaded = json.loads(destination.read_text(encoding="utf-8"))
        assert loaded["app"]["name"]
        assert loaded["_excluded"]

    def test_it_is_indented_for_a_person_to_read(
        self, deps: dict[str, object], tmp_path: Path
    ) -> None:
        destination = write_report(tmp_path / "diagnostics.json", **deps)

        text = destination.read_text(encoding="utf-8")

        assert '\n  "app": {' in text


class TestExportZip:
    def test_it_holds_the_report_and_the_logs(
        self, deps: dict[str, object], app_paths: AppPaths, tmp_path: Path
    ) -> None:
        (app_paths.logs_dir / TEXT_LOG_NAME).write_text("started\n", encoding="utf-8")
        (app_paths.logs_dir / f"{TEXT_LOG_NAME}.1").write_text("older\n", encoding="utf-8")
        write_event_log(app_paths, '{"message": "hello"}')

        archive_path = export_zip(tmp_path / "support.zip", **deps)

        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            assert report_module.REPORT_NAME_IN_ZIP in names
            assert f"logs/{TEXT_LOG_NAME}" in names
            assert f"logs/{TEXT_LOG_NAME}.1" in names
            assert f"logs/{EVENT_LOG_NAME}" in names
            assert json.loads(archive.read(report_module.REPORT_NAME_IN_ZIP))["app"]

    def test_nothing_from_the_browser_profile_is_archived(
        self, deps: dict[str, object], app_paths: AppPaths, tmp_path: Path
    ) -> None:
        profile = app_paths.browser_profile_dir / "Default"
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "Cookies").write_text(COOKIE_VALUE, encoding="utf-8")
        (app_paths.logs_dir / TEXT_LOG_NAME).write_text("started\n", encoding="utf-8")

        archive_path = export_zip(tmp_path / "support.zip", **deps)

        with zipfile.ZipFile(archive_path) as archive:
            for name in archive.namelist():
                assert "browser" not in name.lower()
                assert "amazon-profile" not in name.lower()

    def test_the_browser_profile_is_refused_even_if_a_log_lived_there(
        self, app_paths: AppPaths
    ) -> None:
        inside = app_paths.browser_profile_dir / TEXT_LOG_NAME
        inside.parent.mkdir(parents=True, exist_ok=True)
        inside.write_text("nope\n", encoding="utf-8")

        assert report_module._is_excluded_path(inside, app_paths) is True
        assert (
            report_module._is_excluded_path(
                app_paths.logs_dir / TEXT_LOG_NAME, app_paths
            )
            is False
        )

    def test_the_log_text_is_redacted_inside_the_archive(
        self, deps: dict[str, object], app_paths: AppPaths, tmp_path: Path
    ) -> None:
        (app_paths.logs_dir / TEXT_LOG_NAME).write_text(
            f'password="{PASSWORD_VALUE}" card {CARD_NUMBER}\n', encoding="utf-8"
        )

        archive_path = export_zip(tmp_path / "support.zip", **deps)

        with zipfile.ZipFile(archive_path) as archive:
            text = archive.read(f"logs/{TEXT_LOG_NAME}").decode("utf-8")

        assert PASSWORD_VALUE not in text
        assert CARD_NUMBER not in text
        assert REDACTED in text


class TestClearDiagnostics:
    def test_it_removes_the_files_and_the_rows(
        self,
        app_paths: AppPaths,
        database: Database,
        activity: ActivityRepository,
    ) -> None:
        for name in ("one.png", "two.png"):
            (app_paths.screenshots_dir / name).write_bytes(b"png")
            activity.record_diagnostic(
                step="add_to_cart",
                error_code=None,
                page_url="https://www.amazon.com/dp/B0TEST",
                screenshot_file=name,
            )

        removed = clear_diagnostics(app_paths, activity)

        assert removed == 2
        assert list(app_paths.screenshots_dir.iterdir()) == []
        assert activity.list_diagnostics() == []

    def test_it_also_removes_files_with_no_row(
        self, app_paths: AppPaths, activity: ActivityRepository
    ) -> None:
        (app_paths.screenshots_dir / "orphan.png").write_bytes(b"png")

        assert clear_diagnostics(app_paths, activity) == 1

    def test_a_stored_name_cannot_point_outside_the_screenshots_folder(
        self, app_paths: AppPaths, activity: ActivityRepository
    ) -> None:
        """A row is data, not a path to trust."""
        outside = app_paths.config_dir / "settings.json"
        outside.write_text("{}", encoding="utf-8")
        activity.record_diagnostic(
            step="checkout",
            error_code=None,
            page_url=None,
            screenshot_file="../config/settings.json",
        )

        clear_diagnostics(app_paths, activity)

        assert outside.exists()

    def test_it_reports_zero_when_there_is_nothing_to_remove(
        self, app_paths: AppPaths, activity: ActivityRepository
    ) -> None:
        assert clear_diagnostics(app_paths, activity) == 0
