"""When to check what.

Two problems this solves, both of which cause real misbehaviour if ignored:

**Pacing.** A watch list must not turn into a burst of requests. Beyond each
job's own interval there is a global floor between any two Amazon page loads,
so adding products cannot multiply the request rate, and each interval gets
random jitter so a scheduler does not produce a perfectly regular pattern.

**Resuming from sleep.** A laptop that was closed for six hours wakes up with
several checks overdue. Replaying each missed occurrence would fire a burst
at exactly the moment the network is least ready. Instead the scheduler
re-anchors every job to *now* and coalesces the missed occurrences into one
check each.

The timing decisions live here, separately from :mod:`monitor_service`, so
they can be tested without a browser or a database.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Qt, Signal

from app.config import GLOBAL_MIN_REQUEST_GAP_SECONDS

logger = logging.getLogger("app.monitoring.scheduler")

#: How often the scheduler wakes to look for work. Coarse on purpose: the
#: precision of a price check does not justify waking the CPU more often.
TICK_INTERVAL_MS = 10_000

#: A wall-clock or monotonic jump larger than this means the machine slept.
TIME_JUMP_THRESHOLD_SECONDS = 90.0

#: How many jobs may be claimed per tick. Bounded so one tick cannot queue
#: the entire watch list at once after a long sleep.
MAX_JOBS_PER_TICK = 3


@dataclass(frozen=True)
class PacingDecision:
    """Whether a check may start now."""

    allowed: bool
    wait_seconds: float = 0.0
    reason: str = ""


class RequestPacer:
    """Enforces a minimum gap between Amazon page loads.

    Applies across every watch job rather than per job, so the request rate
    does not grow with the size of the watch list.
    """

    def __init__(self, minimum_gap_seconds: float = GLOBAL_MIN_REQUEST_GAP_SECONDS) -> None:
        self._minimum_gap = minimum_gap_seconds
        self._last_request: float | None = None

    def check(self, *, now: float | None = None) -> PacingDecision:
        current = now if now is not None else time.monotonic()
        if self._last_request is None:
            return PacingDecision(allowed=True)
        elapsed = current - self._last_request
        if elapsed >= self._minimum_gap:
            return PacingDecision(allowed=True)
        return PacingDecision(
            allowed=False,
            wait_seconds=self._minimum_gap - elapsed,
            reason="keeping a polite gap between requests",
        )

    def record_request(self, *, now: float | None = None) -> None:
        self._last_request = now if now is not None else time.monotonic()

    def reset(self) -> None:
        self._last_request = None


class ClockJumpDetector:
    """Detects that the machine slept, or that the clock was changed.

    Both clocks are compared because on Windows the monotonic clock does not
    advance across S3 sleep but does across modern standby, while the wall
    clock advances in every case. Taking the larger of the two deltas catches
    classic sleep, modern standby, hibernation and a clock correction with
    one rule.
    """

    def __init__(
        self,
        *,
        interval_seconds: float = TICK_INTERVAL_MS / 1000,
        threshold_seconds: float = TIME_JUMP_THRESHOLD_SECONDS,
    ) -> None:
        self._interval = interval_seconds
        self._threshold = threshold_seconds
        self._monotonic = time.monotonic()
        self._wall = time.time()

    def evaluate(
        self, monotonic_now: float | None = None, wall_now: float | None = None
    ) -> float | None:
        """The size of the jump, or ``None`` if time passed normally."""
        monotonic_current = (
            monotonic_now if monotonic_now is not None else time.monotonic()
        )
        wall_current = wall_now if wall_now is not None else time.time()

        monotonic_delta = monotonic_current - self._monotonic
        wall_delta = wall_current - self._wall
        self._monotonic = monotonic_current
        self._wall = wall_current

        jump = max(monotonic_delta, wall_delta) - self._interval
        return jump if jump > self._threshold else None


class Scheduler(QObject):
    """A coarse repeating timer with sleep/wake awareness."""

    #: Emitted on each tick, when work should be looked for.
    tick = Signal()
    #: Emitted with the size of the jump when the machine appears to have slept.
    resumed = Signal(float)

    def __init__(
        self,
        *,
        interval_ms: int = TICK_INTERVAL_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        # A coarse timer lets Windows group wake-ups, which matters for
        # battery life in an app that is meant to be left running.
        self._timer.setTimerType(Qt.TimerType.VeryCoarseTimer)
        self._timer.timeout.connect(self._on_tick)
        self._detector = ClockJumpDetector(interval_seconds=interval_ms / 1000)
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._timer.start()
        logger.info("Scheduler started", extra={"interval_ms": self._timer.interval()})
        # Look for work immediately rather than waiting a full interval.
        QTimer.singleShot(0, self._on_tick)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._timer.stop()
        logger.info("Scheduler stopped")

    def _on_tick(self) -> None:
        if not self._running:
            return
        jump = self._detector.evaluate()
        if jump is not None:
            logger.info(
                "Time jumped; rescheduling instead of catching up",
                extra={"jump_seconds": round(jump, 1)},
            )
            self.resumed.emit(jump)
        self.tick.emit()
