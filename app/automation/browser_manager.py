"""Lifecycle of the dedicated Amazon browser.

Key decisions, each with a reason:

*A dedicated profile, never the user's Chrome.* The browser runs against
``%LOCALAPPDATA%\\<app>\\browser\\amazon-profile``. Chrome's own "User Data"
directory is explicitly unsupported for automation -- pointing at it makes
pages fail to load or the browser exit -- and taking over someone's real
profile would be wrong regardless.

*Bundled Chromium, no channel.* Using the browser build that ships with the
pinned Playwright version makes behaviour reproducible. Selecting
``channel="chrome"`` or ``"msedge"`` would hand the app's behaviour to
whatever version, policy and extension set the user's browser happens to have.

*Browsers are installed at first run, not bundled.* Chromium is roughly
430 MB on disk. Installing it into the data directory keeps application
updates small and avoids a several-hundred-megabyte extraction on every
launch. ``--no-shell`` skips the headless shell, which is another 270 MB that
a headed browser can never use.

*Headed, always.* The user has to be able to sign in, complete a one-time
code and see what is happening at checkout. Running hidden would also be the
first step towards pretending not to be automation, which this program does
not do.

*Thread affinity.* Playwright's synchronous API is not thread-safe and its
objects belong to the thread that created them. Every method here that
touches Playwright asserts it is on the owning thread, so a mistake surfaces
as a clear error rather than as intermittent corruption.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.core.errors import AppError, ErrorCode
from app.paths import AppPaths

logger = logging.getLogger("app.automation.browser")

#: Default per-action timeout. Deliberately short: with the synchronous API
#: this is the granularity at which a cancel request can take effect.
DEFAULT_ACTION_TIMEOUT_MS = 15_000
DEFAULT_NAVIGATION_TIMEOUT_MS = 45_000

#: Window size used for the visible browser. Wide enough that Amazon serves
#: the desktop layout the selectors are written against.
VIEWPORT = {"width": 1360, "height": 900}

#: Markers Playwright emits when another process holds the profile.
_PROFILE_IN_USE_MARKERS = (
    "processsingleton",
    "already in use",
    "opening in existing browser session",
    "failed to create a processsingleton",
)


@dataclass(frozen=True)
class BrowserInfo:
    """What the diagnostics screen shows about the browser."""

    installed: bool
    executable_path: str | None
    version: str | None
    profile_dir: str
    browsers_dir: str
    playwright_version: str | None
    running: bool

    @property
    def summary(self) -> str:
        if not self.installed:
            return "Not installed yet"
        return self.version or "Installed"


def configure_browsers_path(paths: AppPaths) -> None:
    """Point Playwright at the application's own browser directory.

    Must be called before Playwright is imported anywhere, because the value
    is read by the Node driver process that Playwright starts, and it must
    match between install time and run time or the browser will not be found.
    """
    paths.playwright_browsers_dir.mkdir(parents=True, exist_ok=True)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(paths.playwright_browsers_dir)


class BrowserManager:
    """Owns the Playwright driver and the persistent browser context."""

    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths
        self._playwright: Any = None
        self._context: Any = None
        self._owner_thread: int | None = None
        self._lock = threading.Lock()

    # ---- installation ----------------------------------------------------


    @property
    def paths(self) -> AppPaths:
        """The application's directories, for callers that cache alongside."""
        return self._paths

    def chromium_installed(self) -> bool:
        """Whether a usable headed Chromium build is present."""
        return self.chromium_executable() is not None

    #: Layouts Playwright has used for the Chromium build directory. Current
    #: builds use ``chrome-win64``; ``chrome-win`` is the older 32-bit-era
    #: name and is kept so an existing install is still recognised.
    _EXECUTABLE_PATTERNS = (
        "chromium-*/chrome-win64/chrome.exe",
        "chromium-*/chrome-win/chrome.exe",
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    )

    def chromium_executable(self) -> Path | None:
        """Path to the bundled Chromium, or ``None`` when not installed.

        When several builds are present the highest build number wins, so an
        upgrade does not keep launching a stale one.
        """
        root = self._paths.playwright_browsers_dir
        if not root.exists():
            return None
        found: list[Path] = []
        for pattern in self._EXECUTABLE_PATTERNS:
            found.extend(
                candidate for candidate in root.glob(pattern) if candidate.is_file()
            )
        if not found:
            return None
        return max(found, key=lambda path: _build_number(path))

    def install_chromium(
        self, on_progress: Callable[[str], None] | None = None
    ) -> None:
        """Download Chromium into the application's browser directory.

        Runs Playwright's own installer the same way ``python -m playwright``
        does: by invoking the bundled Node driver directly. Shelling out to
        ``sys.executable -m playwright`` would not work in a packaged build,
        where ``sys.executable`` is the application's own executable.
        """
        configure_browsers_path(self._paths)
        from playwright._impl._driver import (  # noqa: PLC0415 - lazy by design
            compute_driver_executable,
            get_driver_env,
        )

        node_executable, cli_script = compute_driver_executable()
        command = [
            str(node_executable),
            str(cli_script),
            "install",
            "chromium",
            "--no-shell",
        ]
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        logger.info(
            "Installing browser",
            extra={"target": str(self._paths.playwright_browsers_dir)},
        )
        if on_progress:
            on_progress("Downloading the browser. This happens only once.")

        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                command,
                env=get_driver_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creation_flags,
            )
        except OSError as exc:
            raise AppError(
                ErrorCode.BROWSER_UNAVAILABLE,
                context={"reason": "installer_failed_to_start"},
                cause=exc,
            ) from exc

        output: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            text = line.rstrip()
            if not text:
                continue
            output.append(text)
            logger.debug("Browser installer", extra={"line": text})
            if on_progress and ("%" in text or "Downloading" in text):
                on_progress(_friendly_install_line(text))

        code = process.wait()
        if code != 0 or not self.chromium_installed():
            raise AppError(
                ErrorCode.BROWSER_UNAVAILABLE,
                context={
                    "reason": "installer_failed",
                    "exit_code": code,
                    "last_output": output[-3:],
                },
                detail_override=(
                    "The browser could not be downloaded. Check your internet "
                    "connection and try again."
                ),
            )
        if on_progress:
            on_progress("Browser ready.")
        logger.info("Browser installed")

    # ---- lifecycle -------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._context is not None

    def _assert_owner_thread(self) -> None:
        current = threading.get_ident()
        if self._owner_thread is not None and self._owner_thread != current:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                context={
                    "reason": "browser_used_from_wrong_thread",
                    "owner_thread": self._owner_thread,
                    "current_thread": current,
                },
            )

    def start(self) -> Any:
        """Start the browser and return its persistent context.

        Idempotent: calling it while already running returns the existing
        context. Raises :class:`AppError` if the browser is not installed.
        """
        self._assert_owner_thread()
        if self._context is not None:
            return self._context

        if not self.chromium_installed():
            raise AppError(
                ErrorCode.BROWSER_UNAVAILABLE,
                context={"reason": "chromium_not_installed"},
                detail_override=(
                    "The browser this app uses has not been downloaded yet. "
                    "Use Repair browser in Settings to install it."
                ),
            )

        configure_browsers_path(self._paths)
        profile_dir = self._paths.browser_profile_dir
        profile_dir.mkdir(parents=True, exist_ok=True)

        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        self._owner_thread = threading.get_ident()
        self._playwright = sync_playwright().start()

        try:
            self._context = self._launch(profile_dir)
        except Exception as exc:
            message = str(exc).lower()
            if any(marker in message for marker in _PROFILE_IN_USE_MARKERS):
                logger.warning("Browser profile was in use; attempting recovery")
                self._recover_profile_lock()
                try:
                    self._context = self._launch(profile_dir)
                except Exception as retry_exc:
                    self._stop_driver()
                    raise AppError(
                        ErrorCode.BROWSER_PROFILE_LOCKED,
                        context={"profile": str(profile_dir)},
                        cause=retry_exc,
                    ) from retry_exc
            else:
                self._stop_driver()
                raise AppError(
                    ErrorCode.BROWSER_UNAVAILABLE,
                    context={"reason": "launch_failed"},
                    cause=exc,
                ) from exc

        self._context.set_default_timeout(DEFAULT_ACTION_TIMEOUT_MS)
        self._context.set_default_navigation_timeout(DEFAULT_NAVIGATION_TIMEOUT_MS)
        logger.info(
            "Browser started",
            extra={"profile": str(profile_dir), "pages": len(self._context.pages)},
        )
        return self._context

    def _launch(self, profile_dir: Path) -> Any:
        """Launch the persistent context.

        ``user_data_dir`` must be absolute, and ``--user-data-dir`` must never
        appear in ``args`` -- Playwright manages that flag itself and raises
        if it is passed explicitly.
        """
        return self._playwright.chromium.launch_persistent_context(
            str(profile_dir.resolve()),
            headless=False,
            viewport=VIEWPORT,
            accept_downloads=False,
            args=[
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-session-crashed-bubble",
                "--hide-crash-restore-bubble",
            ],
            ignore_default_args=["--enable-automation"],
        )

    def page(self) -> Any:
        """The working page, creating one if the context has none."""
        self._assert_owner_thread()
        context = self.start()
        pages = [page for page in context.pages if not page.is_closed()]
        if pages:
            return pages[0]
        return context.new_page()

    def close_extra_pages(self) -> None:
        """Close all but the first page.

        Amazon opens new tabs for seller profiles and help pages; leaving them
        open costs memory and confuses the next read.
        """
        self._assert_owner_thread()
        if self._context is None:
            return
        pages = [page for page in self._context.pages if not page.is_closed()]
        for page in pages[1:]:
            try:
                page.close()
            except Exception:  # noqa: BLE001 - a page that will not close is harmless
                logger.debug("Could not close an extra page", exc_info=True)

    def stop(self) -> None:
        """Close the browser cleanly, then stop the driver.

        ``context.close()`` is what makes Chromium flush cookies, history and
        its LevelDB stores. Killing the process instead leaves the profile
        with a crash flag set and can lose the signed-in session, which is the
        one piece of state the user would have to recreate by hand.
        """
        with self._lock:
            context, self._context = self._context, None
            if context is not None:
                try:
                    context.close()
                    logger.info("Browser closed")
                except Exception:  # noqa: BLE001
                    logger.warning("Browser did not close cleanly", exc_info=True)
            self._stop_driver()
            self._owner_thread = None

    def _stop_driver(self) -> None:
        driver, self._playwright = self._playwright, None
        if driver is None:
            return
        try:
            driver.stop()
        except Exception:  # noqa: BLE001
            logger.warning("Playwright driver did not stop cleanly", exc_info=True)

    # ---- profile maintenance --------------------------------------------

    def _recover_profile_lock(self) -> None:
        """Deal with a profile left locked by a previous run.

        On Windows Chromium guards a profile with a *named mutex*, not the
        ``SingletonLock`` file used on POSIX, so there is no stale lock file
        to delete. A lock therefore means a real browser process is still
        alive holding this profile: find it by command line and end it.
        """
        killed = self._terminate_processes_using_profile()
        self._clear_crash_flags()
        if killed:
            # Give Windows a moment to release the mutex before retrying.
            time.sleep(1.0)

    def _terminate_processes_using_profile(self) -> int:
        if sys.platform != "win32":
            return 0
        needle = str(self._paths.browser_profile_dir.resolve()).lower()
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
        )
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            logger.debug("Could not enumerate browser processes", exc_info=True)
            return 0

        try:
            payload = json.loads(completed.stdout or "[]")
        except ValueError:
            return 0
        rows = payload if isinstance(payload, list) else [payload]

        killed = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            command_line = str(row.get("CommandLine") or "").lower()
            if needle not in command_line:
                continue
            pid = row.get("ProcessId")
            if not pid:
                continue
            try:
                subprocess.run(  # noqa: S603 - fixed argv, no shell
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                killed += 1
            except (OSError, subprocess.SubprocessError):
                continue
        if killed:
            logger.info(
                "Ended browser processes holding the profile",
                extra={"count": killed},
            )
        return killed

    def _clear_crash_flags(self) -> None:
        """Stop Chromium showing "Restore pages?" after an unclean exit.

        The preferences file is an undocumented internal format, so this is
        best-effort: a failure here only costs a dialog the user can dismiss.
        """
        preferences = self._paths.browser_profile_dir / "Default" / "Preferences"
        if not preferences.is_file():
            return
        try:
            data = json.loads(preferences.read_text(encoding="utf-8"))
            profile = data.setdefault("profile", {})
            profile["exit_type"] = "Normal"
            profile["exited_cleanly"] = True
            preferences.write_text(json.dumps(data), encoding="utf-8")
        except (OSError, ValueError, TypeError):
            logger.debug("Could not reset the browser crash flag", exc_info=True)

    def clear_profile(self) -> None:
        """Delete the stored session. The browser must be stopped first.

        This is what "Clear browser session" does in Settings: it removes
        every cookie and stored credential from the app's own profile, so the
        next connection starts from a signed-out browser. Nothing outside the
        application's data directory is touched.
        """
        if self.is_running:
            self.stop()
        self._terminate_processes_using_profile()
        profile_dir = self._paths.browser_profile_dir
        if profile_dir.exists():
            shutil.rmtree(profile_dir, ignore_errors=True)
        profile_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Browser profile cleared")

    def profile_size_bytes(self) -> int:
        total = 0
        for path in self._paths.browser_profile_dir.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    def has_stored_session(self) -> bool:
        """Whether the profile holds anything that looks like a session.

        Used only to decide what to show before the first connection; the
        authoritative answer comes from actually loading a page.
        """
        cookies = self._paths.browser_profile_dir / "Default" / "Cookies"
        return cookies.is_file() and cookies.stat().st_size > 0

    # ---- diagnostics -----------------------------------------------------

    def info(self) -> BrowserInfo:
        """A snapshot for the Settings and diagnostics screens."""
        executable = self.chromium_executable()
        version: str | None = None
        if executable is not None:
            # The build number is in the directory name, e.g. chromium-1243.
            version = executable.parent.parent.name.replace("chromium-", "Chromium build ")
        playwright_version: str | None = None
        try:
            from importlib.metadata import version as package_version  # noqa: PLC0415

            playwright_version = package_version("playwright")
        except Exception:  # noqa: BLE001
            playwright_version = None

        return BrowserInfo(
            installed=executable is not None,
            executable_path=str(executable) if executable else None,
            version=version,
            profile_dir=str(self._paths.browser_profile_dir),
            browsers_dir=str(self._paths.playwright_browsers_dir),
            playwright_version=playwright_version,
            running=self.is_running,
        )


def _friendly_install_line(text: str) -> str:
    """Turn installer output into something worth showing a person."""
    stripped = text.strip()
    if "%" in stripped:
        for token in stripped.split():
            if token.endswith("%"):
                return f"Downloading the browser... {token}"
    return "Downloading the browser. This happens only once."


def _build_number(executable: Path) -> int:
    """Playwright's build number from a path like ``chromium-1243/...``.

    Used to pick the newest installed build. Returns 0 for an unrecognised
    directory name so it sorts last rather than raising.
    """
    for part in executable.parts:
        if part.startswith("chromium-"):
            suffix = part.removeprefix("chromium-")
            if suffix.isdigit():
                return int(suffix)
    return 0
