"""The single thread that owns the browser.

Playwright's synchronous API is not thread-safe and its objects belong to the
thread that created them, so all browser work happens on one dedicated
thread, fed by a queue. That has a second benefit which matters more than the
technical one: **only one browser operation can run at a time**, so a
scheduled price check can never interleave with a checkout, and two checkouts
can never overlap.

The thread has no Qt event loop -- it spends its life blocked in
``queue.get()`` or inside a Playwright call -- so it cannot be driven with
slots. Work is submitted through :meth:`BrowserWorker.submit`, which is
thread-safe, and results come back as Qt signals that Qt delivers to the GUI
thread automatically.

Cancellation is cooperative. A task checks in at each named step, so the
worst-case delay after the user clicks Cancel is one step, bounded by the
per-action timeout. There is no safe way to interrupt a blocked Playwright
call from another thread, and pretending otherwise would risk desynchronising
the driver mid-checkout.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from app.automation.browser_manager import BrowserManager
from app.automation.page_reader import PageReader
from app.core.errors import AppError, ErrorCode, OperationCancelled
from app.paths import AppPaths

logger = logging.getLogger("app.automation.worker")


class Priority(IntEnum):
    """Queue ordering. Lower runs first.

    Anything the user is waiting for outranks background monitoring, so a
    watch list with work queued never makes the app feel unresponsive.
    """

    INTERACTIVE = 0
    PURCHASE = 1
    MONITORING = 5


class WorkerState(StrEnum):
    """What the worker is doing, for the UI and the tray icon."""

    STOPPED = "stopped"
    STARTING = "starting"
    IDLE = "idle"
    BUSY = "busy"
    NEEDS_BROWSER = "needs_browser"
    FAILED = "failed"

    @property
    def label(self) -> str:
        return {
            WorkerState.STOPPED: "Stopped",
            WorkerState.STARTING: "Starting the browser",
            WorkerState.IDLE: "Ready",
            WorkerState.BUSY: "Working",
            WorkerState.NEEDS_BROWSER: "Browser needs installing",
            WorkerState.FAILED: "Browser problem",
        }[self]


class BrowserSession:
    """What a task is given to work with.

    Deliberately thin: it exposes the page, a reader, and the two things a
    task must do to be well-behaved -- report progress and check for
    cancellation.
    """

    def __init__(self, worker: BrowserWorker, task: BrowserTask) -> None:
        self._worker = worker
        self._task = task

    @property
    def manager(self) -> BrowserManager:
        return self._worker.manager

    @property
    def page(self) -> Any:
        return self._worker.manager.page()

    @property
    def paths(self) -> AppPaths:
        """Where this run may cache things, such as a product thumbnail."""
        return self._worker.manager.paths

    @property
    def reader(self) -> PageReader:
        """A fresh reader for the current page.

        Fresh each time so the record of which selectors needed a fallback
        belongs to the step being performed rather than accumulating.
        """
        return PageReader(self.page)

    @property
    def task(self) -> BrowserTask:
        return self._task

    def step(self, message: str) -> None:
        """Report progress, and abort if the user has cancelled.

        Called at every meaningful boundary. This is the whole cancellation
        mechanism, so a task that never calls it cannot be cancelled.
        """
        self.check_cancelled()
        self._worker.report_progress(self._task.task_id, message)

    def check_cancelled(self) -> None:
        if self._task.cancel.is_set():
            raise OperationCancelled(self._task.label)

    def show_browser(self) -> None:
        """Bring the browser window to the front for the user to act in."""
        try:
            self.page.bring_to_front()
        except Exception:  # noqa: BLE001 - a window that will not raise is not fatal
            logger.debug("Could not bring the browser to the front", exc_info=True)

    def sleep(self, seconds: float) -> None:
        """Wait, remaining cancellable.

        Sleeps in short slices so a cancel takes effect promptly rather than
        after the whole interval.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check_cancelled()
            remaining = deadline - time.monotonic()
            try:
                self.page.wait_for_timeout(min(0.5, max(remaining, 0)) * 1000)
            except Exception:  # noqa: BLE001
                time.sleep(min(0.5, max(remaining, 0)))


@dataclass
class BrowserTask:
    """One unit of browser work."""

    task_id: int
    label: str
    run: Callable[[BrowserSession], Any]
    priority: Priority = Priority.INTERACTIVE
    cancel: threading.Event = field(default_factory=threading.Event)
    #: Opaque value echoed back with the result, so a caller can correlate.
    context: Any = None


class BrowserWorker(QObject):
    """Runs browser tasks on a dedicated thread, one at a time."""

    #: task_id, human-readable step
    progress = Signal(int, str)
    #: task_id, result, context
    finished = Signal(int, object, object)
    #: task_id, AppError, context
    failed = Signal(int, object, object)
    #: task_id, context -- cancellation is not a failure
    cancelled = Signal(int, object)
    #: WorkerState value
    state_changed = Signal(str)
    #: Emitted when the browser needs installing, with a progress message.
    install_progress = Signal(str)

    def __init__(self, paths: AppPaths, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._manager = BrowserManager(paths)
        self._queue: queue.PriorityQueue[tuple[int, int, BrowserTask | None]] = (
            queue.PriorityQueue()
        )
        self._sequence = itertools.count()
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._current: BrowserTask | None = None
        self._pending: dict[int, BrowserTask] = {}
        self._state = WorkerState.STOPPED
        self._stopping = threading.Event()

    # ---- properties ------------------------------------------------------

    @property
    def manager(self) -> BrowserManager:
        return self._manager

    @property
    def state(self) -> WorkerState:
        return self._state

    @property
    def queue_depth(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._current is not None

    # ---- submission (called from the GUI thread) -------------------------

    def submit(
        self,
        label: str,
        run: Callable[[BrowserSession], Any],
        *,
        priority: Priority = Priority.INTERACTIVE,
        context: Any = None,
    ) -> int:
        """Queue a task and return its id. Thread-safe."""
        task = BrowserTask(
            task_id=next(self._ids),
            label=label,
            run=run,
            priority=priority,
            context=context,
        )
        with self._lock:
            self._pending[task.task_id] = task
        self._queue.put((int(priority), next(self._sequence), task))
        logger.debug(
            "Task queued",
            extra={"task_id": task.task_id, "label": label, "priority": int(priority)},
        )
        return task.task_id

    def cancel(self, task_id: int) -> bool:
        """Ask a task to stop. Returns whether it was known."""
        with self._lock:
            task = self._pending.get(task_id)
            if task is None and self._current is not None and self._current.task_id == task_id:
                task = self._current
        if task is None:
            return False
        task.cancel.set()
        logger.info("Cancellation requested", extra={"task_id": task_id})
        return True

    def cancel_all(self, *, only_priority: Priority | None = None) -> int:
        """Cancel every queued and running task, or just one priority band."""
        with self._lock:
            tasks = list(self._pending.values())
            if self._current is not None:
                tasks.append(self._current)
        count = 0
        for task in tasks:
            if only_priority is not None and task.priority != only_priority:
                continue
            task.cancel.set()
            count += 1
        return count

    def shutdown(self) -> None:
        """Ask the worker to finish and close the browser."""
        self._stopping.set()
        self.cancel_all()
        # Highest priority sentinel so it is taken as soon as the current task
        # finishes, ahead of anything still queued.
        self._queue.put((-1, next(self._sequence), None))

    # ---- the worker thread ----------------------------------------------

    @Slot()
    def run(self) -> None:
        """The thread body. Connected to ``QThread.started``."""
        self._set_state(WorkerState.STARTING)
        browser_ready = True
        try:
            self._ensure_browser()
        except AppError as error:
            browser_ready = False
            self._set_state(
                WorkerState.NEEDS_BROWSER
                if error.code is ErrorCode.BROWSER_UNAVAILABLE
                else WorkerState.FAILED
            )
            logger.error("Browser could not be prepared", exc_info=error)
            # Keep serving the queue: every task will fail with a clear
            # message, which is better than silently swallowing requests, and
            # the user can repair the browser from Settings.

        # Only report readiness when the browser is genuinely ready. Setting
        # IDLE unconditionally here would overwrite NEEDS_BROWSER one line
        # after it was set, so the status bar would claim "Ready" while
        # nothing could work.
        if browser_ready:
            self._set_state(WorkerState.IDLE)
        try:
            self._loop()
        finally:
            self._manager.stop()
            self._drain_queue()
            self._set_state(WorkerState.STOPPED)
            logger.info("Browser worker stopped")

    def _drain_queue(self) -> None:
        """Report every task still queued as cancelled, so nothing is lost.

        Without this, a shutdown (or a thread that exited unexpectedly) would
        leave callers waiting on a signal that can never arrive, and any
        purchase job behind it stuck in a live state.
        """
        while True:
            try:
                _priority, _sequence, task = self._queue.get_nowait()
            except queue.Empty:
                break
            if task is None:
                continue
            logger.info(
                "Task abandoned because the worker stopped",
                extra={"task_id": task.task_id, "label": task.label},
            )
            self.cancelled.emit(task.task_id, task.context)
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for task in pending:
            self.cancelled.emit(task.task_id, task.context)

    def _loop(self) -> None:
        while True:
            _priority, _sequence, task = self._queue.get()
            if task is None:
                return
            with self._lock:
                self._pending.pop(task.task_id, None)
                self._current = task
            self._set_state(WorkerState.BUSY)
            try:
                self._execute(task)
            finally:
                with self._lock:
                    self._current = None
                if not self._stopping.is_set():
                    self._set_state(WorkerState.IDLE)

    def _execute(self, task: BrowserTask) -> None:
        logger.info("Task started", extra={"task_id": task.task_id, "label": task.label})
        started = time.monotonic()
        session = BrowserSession(self, task)
        try:
            if task.cancel.is_set():
                raise OperationCancelled(task.label)
            self._ensure_browser()
            result = task.run(session)
        except OperationCancelled:
            logger.info("Task cancelled", extra={"task_id": task.task_id})
            self.cancelled.emit(task.task_id, task.context)
            return
        except AppError as error:
            logger.warning(
                "Task failed",
                extra={
                    "task_id": task.task_id,
                    "label": task.label,
                    "code": error.code.value,
                    "context": error.context,
                },
            )
            self.failed.emit(task.task_id, error, task.context)
            return
        except BaseException as exc:  # noqa: BLE001 - see below
            # BaseException, not Exception. A KeyboardInterrupt or a
            # SystemExit escaping here would kill the worker thread, and
            # because the thread is the only thing servicing the queue, every
            # later task would be enqueued and never run, never fail and
            # never report -- leaving the in-flight purchase job live, which
            # blocks that product permanently. Reporting first and re-raising
            # after keeps the caller informed either way.
            wrapped = self._wrap_unexpected(exc, task)
            logger.exception(
                "Task raised an unexpected error",
                extra={"task_id": task.task_id, "label": task.label},
            )
            self.failed.emit(task.task_id, wrapped, task.context)
            # Deliberately not re-raised. This thread is the only thing that
            # services the queue, so letting anything escape would silently
            # stop all monitoring and leave the app looking alive while doing
            # nothing. Shutdown is requested explicitly, through the queue
            # sentinel, so refusing to die here cannot prevent exiting.
            return
        finally:
            self._tidy_up()

        logger.info(
            "Task finished",
            extra={
                "task_id": task.task_id,
                "label": task.label,
                "seconds": round(time.monotonic() - started, 2),
            },
        )
        self.finished.emit(task.task_id, result, task.context)

    def _wrap_unexpected(self, exc: BaseException, task: BrowserTask) -> AppError:
        """Translate a Playwright or runtime error into a user-facing one."""
        message = str(exc).lower()
        name = type(exc).__name__

        if "timeout" in message or name == "TimeoutError":
            code = ErrorCode.TIMEOUT
        elif any(
            marker in message
            for marker in (
                "net::err_internet_disconnected",
                "net::err_name_not_resolved",
                "net::err_connection",
                "net::err_address_unreachable",
            )
        ):
            code = ErrorCode.NETWORK_UNAVAILABLE
        elif "target page, context or browser has been closed" in message or (
            "browser has been closed" in message
        ):
            code = ErrorCode.BROWSER_CLOSED_BY_USER
        elif "executable doesn't exist" in message:
            code = ErrorCode.BROWSER_UNAVAILABLE
        else:
            code = ErrorCode.INTERNAL_ERROR

        return AppError(
            code,
            context={"step": task.label, "exception": name},
            cause=exc,
        )

    def _ensure_browser(self) -> None:
        """Install the browser if needed, then start it."""
        if not self._manager.chromium_installed():
            self.install_progress.emit("Setting up the browser...")
            self._manager.install_chromium(on_progress=self.install_progress.emit)
        self._manager.start()

    def _tidy_up(self) -> None:
        """Keep memory and tab count down between tasks."""
        try:
            self._manager.close_extra_pages()
        except Exception:  # noqa: BLE001
            logger.debug("Could not tidy up extra pages", exc_info=True)

    # ---- helpers used by BrowserSession ----------------------------------

    def report_progress(self, task_id: int, message: str) -> None:
        self.progress.emit(task_id, message)

    def _set_state(self, state: WorkerState) -> None:
        if state is self._state:
            return
        self._state = state
        self.state_changed.emit(state.value)
