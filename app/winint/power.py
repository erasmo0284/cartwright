"""Noticing that time moved when the application was not looking.

A laptop that sleeps for two hours, a machine that hibernates overnight and a
clock that a time server steps forward all look the same from inside a running
process: the next timer tick arrives far later than it was scheduled. If the
scheduler simply caught up it would fire every check it missed at once, which
means a burst of requests the moment the lid opens. The correct response is to
throw away the old schedule and work out afresh when the next check is due,
so this watcher only reports the jump and leaves that decision to the
scheduler.

Both clocks are compared, and the larger gap wins, because neither alone
covers every case on Windows:

* ``time.monotonic()`` is not advanced across classic S3 sleep, so it misses
  that kind of sleep entirely, but it does advance across modern standby and
  is immune to clock changes.
* ``time.time()`` advances in every case, including sleep and hibernate, but
  it also moves when a time server corrects the clock or the user changes the
  time zone offset.

Taking the maximum of the two gaps catches classic sleep, modern standby,
hibernate and a clock step with a single rule, and needs no Windows-specific
power notification window, which would not work in a console or on another
operating system.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from app.diagnostics.logger import get_logger

logger = get_logger("winint.power")


class ClockWatcher(QObject):
    """Emits :attr:`time_jumped` when far more time passed than expected.

    The polling interval is deliberately coarse: this is a background sanity
    check, not a timer anything depends on, and a very coarse timer lets
    Windows coalesce it with other wake-ups instead of waking the CPU on its
    own schedule.
    """

    #: Seconds that appear to have passed since the previous tick.
    time_jumped = Signal(float)

    def __init__(
        self,
        interval_ms: int = 5000,
        threshold_seconds: float = 30.0,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._interval_ms = interval_ms
        self._interval_seconds = interval_ms / 1000.0
        self._threshold_seconds = threshold_seconds
        self._last_monotonic = time.monotonic()
        self._last_wall = time.time()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.VeryCoarseTimer)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._on_timeout)

    def start(self) -> None:
        """Begin watching. Resets the baseline so a pause is not a jump."""
        self._last_monotonic = time.monotonic()
        self._last_wall = time.time()
        self._timer.start()

    def stop(self) -> None:
        """Stop watching."""
        self._timer.stop()

    def evaluate(self, monotonic_now: float, wall_now: float) -> float | None:
        """Gap in seconds when this tick looks like a jump, else ``None``.

        Kept free of the timer and of the current time so the rule can be
        exercised directly, without an event loop and without waiting.
        """
        elapsed = max(
            monotonic_now - self._last_monotonic,
            wall_now - self._last_wall,
        )
        self._last_monotonic = monotonic_now
        self._last_wall = wall_now
        if elapsed - self._interval_seconds > self._threshold_seconds:
            return elapsed
        return None

    def _on_timeout(self) -> None:
        elapsed = self.evaluate(time.monotonic(), time.time())
        if elapsed is None:
            return
        logger.info("The computer was asleep or the clock changed", extra={"gap_seconds": round(elapsed, 1)})
        self.time_jumped.emit(elapsed)
