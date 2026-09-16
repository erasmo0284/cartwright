"""Tests for :mod:`app.winint`.

The registry-touching tests are redirected to a scratch key under
``HKCU\\Software\\<data folder>Test`` and delete it again afterwards, so they
never read or write the real ``Run``, ``StartupApproved`` or
``AppUserModelId`` keys. Nothing here shows a window: Qt runs on the
offscreen platform.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from app.branding import BRAND
from app.winint import aumid, startup
from app.winint.power import ClockWatcher
from app.winint.single_instance import SingleInstance, per_user_key
from app.winint.tray import MAX_TOOLTIP_CHARS, TrayController, build_tooltip

ON_WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not ON_WINDOWS, reason="Windows registry only")

SCRATCH_BASE = rf"Software\{BRAND.data_folder_name}Test"


@pytest.fixture(scope="module")
def qt_app() -> Iterator[QApplication]:
    """A single offscreen QApplication for every test that needs Qt."""
    app = QApplication.instance() or QApplication([])
    yield app  # type: ignore[misc]


def _delete_tree(path: str) -> None:
    """Remove a scratch key and everything under it."""
    import winreg

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ | winreg.KEY_WRITE
        )
    except FileNotFoundError:
        return
    with key:
        while True:
            try:
                child = winreg.EnumKey(key, 0)
            except OSError:
                break
            _delete_tree(f"{path}\\{child}")
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:  # pragma: no cover - only if another process holds the key
        pass


@pytest.fixture
def scratch_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Redirect every registry path in :mod:`app.winint` to a scratch key."""
    monkeypatch.setattr(startup, "_RUN_KEY", rf"{SCRATCH_BASE}\Run")
    monkeypatch.setattr(
        startup, "_APPROVED_KEY", rf"{SCRATCH_BASE}\StartupApproved\Run"
    )
    monkeypatch.setattr(aumid, "_AUMID_KEY_ROOT", rf"{SCRATCH_BASE}\AppUserModelId")
    yield SCRATCH_BASE
    if ON_WINDOWS:
        _delete_tree(SCRATCH_BASE)


def _write_approved_blob(first_byte: int) -> None:
    """Fake what Windows writes when the user flips the startup switch."""
    import winreg

    blob = bytes([first_byte]) + bytes(11)
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, startup._APPROVED_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(
            key, BRAND.startup_registry_value, 0, winreg.REG_BINARY, blob
        )


class TestStartupCommand:
    def test_includes_tray_flag_and_quotes_the_program(self) -> None:
        command = startup.startup_command()
        assert command.startswith('"')
        assert command.endswith("--tray")

    def test_path_with_spaces_stays_quoted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        exe = tmp_path / "Program Folder" / "app.exe"
        monkeypatch.setattr(startup, "is_frozen", lambda: True)
        monkeypatch.setattr(startup, "executable_path", lambda: exe)
        command = startup.startup_command()
        assert command == f'"{exe}" --tray'
        assert "--tray" in command

    def test_from_source_runs_the_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(startup, "is_frozen", lambda: False)
        command = startup.startup_command()
        assert "-m app.main" in command


class TestStartupApprovedBlob:
    @pytest.mark.parametrize("first_byte", [0x02, 0x06])
    def test_enabled_bytes(self, first_byte: int) -> None:
        assert startup.is_disabled_blob(bytes([first_byte]) + bytes(11)) is False

    @pytest.mark.parametrize("first_byte", [0x03, 0x07])
    def test_disabled_bytes(self, first_byte: int) -> None:
        assert startup.is_disabled_blob(bytes([first_byte]) + bytes(11)) is True

    def test_unexpected_shapes_are_not_treated_as_disabled(self) -> None:
        assert startup.is_disabled_blob(b"") is False
        assert startup.is_disabled_blob(None) is False
        assert startup.is_disabled_blob("0x03") is False


@windows_only
class TestStartupRegistry:
    def test_enable_then_disable(self, scratch_registry: str) -> None:
        startup.enable()
        assert startup.is_enabled() is True

        state = startup.status()
        assert state.registered is True
        assert state.disabled_by_windows is False
        assert state.effective is True
        assert state.command is not None
        assert "--tray" in state.command
        assert state.summary == "On"

        startup.disable()
        assert startup.is_enabled() is False
        assert startup.status().registered is False
        assert startup.status().summary == "Off"

    def test_disable_when_absent_does_not_raise(self, scratch_registry: str) -> None:
        startup.disable()
        startup.disable()
        assert startup.is_enabled() is False

    def test_enable_is_idempotent(self, scratch_registry: str) -> None:
        startup.enable()
        first = startup.status().command
        startup.enable()
        assert startup.status().command == first

    def test_disabled_in_windows_settings_beats_a_present_run_value(
        self, scratch_registry: str
    ) -> None:
        startup.enable()
        _write_approved_blob(0x03)

        state = startup.status()
        assert state.registered is True
        assert state.disabled_by_windows is True
        assert state.effective is False
        assert state.summary == "Turned off in Windows Settings"
        assert startup.is_enabled() is False

    def test_approved_blob_marked_enabled_leaves_autostart_on(
        self, scratch_registry: str
    ) -> None:
        startup.enable()
        _write_approved_blob(0x02)
        assert startup.status().effective is True


@windows_only
class TestAumid:
    def test_register_and_unregister(self, scratch_registry: str) -> None:
        assert aumid.is_registered() is False
        assert aumid.register_aumid() is True
        assert aumid.is_registered() is True
        assert aumid.display_name() == BRAND.display_name

        aumid.unregister_aumid()
        assert aumid.is_registered() is False

    def test_unregister_when_absent_does_not_raise(
        self, scratch_registry: str
    ) -> None:
        aumid.unregister_aumid()
        assert aumid.is_registered() is False

    def test_icon_is_recorded_only_when_the_file_exists(
        self, scratch_registry: str, tmp_path: Path
    ) -> None:
        import winreg

        missing = tmp_path / "nope.ico"
        assert aumid.register_aumid(missing) is True
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, aumid._key_path(), 0, winreg.KEY_READ
        ) as key:
            with pytest.raises(FileNotFoundError):
                winreg.QueryValueEx(key, "IconUri")

        present = tmp_path / "icon.ico"
        present.write_bytes(b"not a real icon")
        assert aumid.register_aumid(present) is True
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, aumid._key_path(), 0, winreg.KEY_READ
        ) as key:
            recorded, _ = winreg.QueryValueEx(key, "IconUri")
            background, _ = winreg.QueryValueEx(key, "IconBackgroundColor")
        assert Path(recorded) == present.resolve()
        assert background == "0"


class TestClockWatcher:
    """The jump rule is exercised directly, with no timer and no waiting."""

    def test_normal_tick_is_not_a_jump(self, qt_app: QApplication) -> None:
        watcher = ClockWatcher(interval_ms=5000, threshold_seconds=30.0)
        base_mono, base_wall = 1000.0, 2000.0
        watcher.evaluate(base_mono, base_wall)
        assert watcher.evaluate(base_mono + 5.0, base_wall + 5.0) is None

    def test_sleep_of_ten_minutes_is_a_jump(self, qt_app: QApplication) -> None:
        watcher = ClockWatcher(interval_ms=5000, threshold_seconds=30.0)
        base_mono, base_wall = 1000.0, 2000.0
        watcher.evaluate(base_mono, base_wall)
        elapsed = watcher.evaluate(base_mono + 600.0, base_wall + 600.0)
        assert elapsed is not None
        assert 595.0 <= elapsed <= 605.0

    def test_wall_clock_step_alone_is_enough(self, qt_app: QApplication) -> None:
        """Classic sleep leaves the monotonic clock unmoved; wall time moves."""
        watcher = ClockWatcher(interval_ms=5000, threshold_seconds=30.0)
        watcher.evaluate(1000.0, 2000.0)
        elapsed = watcher.evaluate(1005.0, 2600.0)
        assert elapsed is not None
        assert 595.0 <= elapsed <= 605.0

    def test_monotonic_step_alone_is_enough(self, qt_app: QApplication) -> None:
        """Modern standby advances the monotonic clock."""
        watcher = ClockWatcher(interval_ms=5000, threshold_seconds=30.0)
        watcher.evaluate(1000.0, 2000.0)
        elapsed = watcher.evaluate(1600.0, 2005.0)
        assert elapsed is not None
        assert 595.0 <= elapsed <= 605.0

    def test_signal_fires_on_a_jumped_tick(
        self, qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        watcher = ClockWatcher(interval_ms=5000, threshold_seconds=30.0)
        seen: list[float] = []
        watcher.time_jumped.connect(seen.append)

        mono, wall = time.monotonic(), time.time()
        monkeypatch.setattr(time, "monotonic", lambda: mono + 5.0)
        monkeypatch.setattr(time, "time", lambda: wall + 5.0)
        watcher._on_timeout()
        assert seen == []

        monkeypatch.setattr(time, "monotonic", lambda: mono + 605.0)
        monkeypatch.setattr(time, "time", lambda: wall + 605.0)
        watcher._on_timeout()
        assert len(seen) == 1
        assert 595.0 <= seen[0] <= 605.0

    def test_start_resets_the_baseline(self, qt_app: QApplication) -> None:
        watcher = ClockWatcher(interval_ms=5000, threshold_seconds=30.0)
        watcher.start()
        watcher.stop()
        assert watcher.evaluate(time.monotonic(), time.time()) is None


class TestSingleInstanceKey:
    def test_key_is_per_user_and_hides_the_user_name(self) -> None:
        key = per_user_key()
        assert key.startswith(BRAND.single_instance_key)
        assert len(key) > len(BRAND.single_instance_key) + 1

    def test_a_different_base_gives_a_different_key(self) -> None:
        assert per_user_key("a") != per_user_key("b")


class TestSingleInstance:
    @pytest.fixture
    def key(self) -> str:
        return f"{BRAND.single_instance_key}.test.{os.getpid()}.{uuid4().hex[:8]}"

    def test_first_copy_wins_and_second_is_turned_away(
        self, qt_app: QApplication, key: str
    ) -> None:
        first = SingleInstance(key=key)
        second = SingleInstance(key=key)
        try:
            assert first.acquire() is True

            received: list[str] = []
            first.activate_requested.connect(received.append)

            assert second.acquire("activate") is False

            deadline = time.monotonic() + 2.0
            while not received and time.monotonic() < deadline:
                QCoreApplication.processEvents()
                time.sleep(0.01)

            assert received == ["activate"], f"last error: {second.last_error!r}"
        finally:
            second.release()
            first.release()

    def test_acquire_is_idempotent_for_the_owner(
        self, qt_app: QApplication, key: str
    ) -> None:
        guard = SingleInstance(key=key)
        try:
            assert guard.acquire() is True
            assert guard.acquire() is True
        finally:
            guard.release()

    def test_release_can_be_called_twice_and_frees_the_name(
        self, qt_app: QApplication, key: str
    ) -> None:
        first = SingleInstance(key=key)
        assert first.acquire() is True
        first.release()
        first.release()

        second = SingleInstance(key=key)
        try:
            assert second.acquire() is True
        finally:
            second.release()

    def test_an_oversized_payload_is_capped(
        self, qt_app: QApplication, key: str
    ) -> None:
        first = SingleInstance(key=key)
        second = SingleInstance(key=key)
        try:
            assert first.acquire() is True
            received: list[str] = []
            first.activate_requested.connect(received.append)

            assert second.acquire("a" * 10_000) is False

            deadline = time.monotonic() + 2.0
            while not received and time.monotonic() < deadline:
                QCoreApplication.processEvents()
                time.sleep(0.01)

            assert received, f"last error: {second.last_error!r}"
            assert len(received[0]) <= 4096
        finally:
            second.release()
            first.release()


class TestTray:
    def test_is_available_returns_a_bool(self, qt_app: QApplication) -> None:
        assert isinstance(TrayController.is_available(), bool)

    def test_tooltip_is_updated_and_within_the_windows_limit(
        self, qt_app: QApplication
    ) -> None:
        controller = TrayController(lambda state: QIcon())
        controller.set_state(
            watching=4, needs_attention=2, paused=False, monitoring_enabled=True
        )
        tooltip = controller.tooltip()
        assert "Watching: 4" in tooltip
        assert "Needs attention: 2" in tooltip
        assert len(tooltip) <= MAX_TOOLTIP_CHARS

    def test_paused_state_is_shown_and_reflected_in_the_menu(
        self, qt_app: QApplication
    ) -> None:
        states: list[str] = []
        controller = TrayController(lambda state: states.append(state) or QIcon())
        controller.set_state(
            watching=1, needs_attention=0, paused=True, monitoring_enabled=True
        )
        assert "Monitoring is paused" in controller.tooltip()
        assert controller._pause_action.isChecked() is True
        assert "paused" in states

    def test_setting_the_paused_state_does_not_look_like_a_user_click(
        self, qt_app: QApplication
    ) -> None:
        controller = TrayController(lambda state: QIcon())
        toggles: list[bool] = []
        controller.pause_toggled.connect(toggles.append)
        controller.set_state(
            watching=0, needs_attention=0, paused=True, monitoring_enabled=True
        )
        assert toggles == []

    def test_long_tooltip_is_truncated(self) -> None:
        tooltip = build_tooltip(
            watching=10**200,
            needs_attention=10**200,
            paused=True,
            monitoring_enabled=True,
        )
        assert len(tooltip) == MAX_TOOLTIP_CHARS
