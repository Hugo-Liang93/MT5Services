"""Lightweight scheduler for notification background jobs.

Two job types, no dependencies beyond ``threading`` / ``datetime``:

- :class:`DailyAtJob` — fires once per day at a fixed ``HH:MM`` UTC.
- :class:`IntervalJob` — fires every N seconds.

Design notes:
- Single worker thread, 1-second tick — enough granularity for minute-level
  scheduling without the cost of a full scheduler framework.
- Jobs are skipped (not queued) if they fall behind schedule; this avoids a
  "thundering herd" of catch-up runs after a process wakes from sleep.
- Exceptions inside a job are caught and logged — one bad job must not kill
  the worker or starve other jobs.
- Stop is cooperative: ``_stop_event.wait(1.0)`` instead of ``time.sleep``.
- ADR-005: after ``stop()`` with timeout, a still-alive thread reference is
  kept so ``start()`` refuses to spawn a second worker.
"""

from __future__ import annotations

import abc
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


Job = Callable[[], None]


@dataclass
class ScheduledJob(abc.ABC):
    name: str
    job: Job

    @abc.abstractmethod
    def is_due(self, *, now: datetime) -> bool:
        raise NotImplementedError

    @abc.abstractmethod
    def mark_fired(self, *, now: datetime) -> None:
        raise NotImplementedError


@dataclass
class DailyAtJob(ScheduledJob):
    hour_utc: int = 0
    minute_utc: int = 0
    _last_fired_date: Optional[str] = field(default=None, repr=False)

    def is_due(self, *, now: datetime) -> bool:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if self._last_fired_date == today:
            return False
        target = now.replace(
            hour=self.hour_utc, minute=self.minute_utc, second=0, microsecond=0
        )
        # Due iff we've passed today's target. Running slightly-late fires the
        # same day (desired); skipping entirely to tomorrow is avoided.
        return now >= target

    def mark_fired(self, *, now: datetime) -> None:
        self._last_fired_date = now.strftime("%Y-%m-%d")


@dataclass
class IntervalJob(ScheduledJob):
    interval_seconds: float = 60.0
    _next_fire_at: Optional[datetime] = field(default=None, repr=False)

    def is_due(self, *, now: datetime) -> bool:
        if self._next_fire_at is None:
            # First tick: fire on next scheduled run, not immediately. Avoids
            # a burst of "every job fires at startup" behavior.
            self._next_fire_at = now + timedelta(seconds=self.interval_seconds)
            return False
        return now >= self._next_fire_at

    def mark_fired(self, *, now: datetime) -> None:
        # Schedule the next run strictly forward, even if we fell behind —
        # prevents a flurry of catch-up calls (which is rarely what we want
        # for polling-style jobs).
        self._next_fire_at = now + timedelta(seconds=self.interval_seconds)


class NotificationScheduler:
    def __init__(
        self,
        *,
        tick_seconds: float = 1.0,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if tick_seconds <= 0:
            raise ValueError("tick_seconds must be > 0")
        self._tick_seconds = float(tick_seconds)
        self._clock = clock
        self._jobs: List[ScheduledJob] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── public: registration ──

    def add_daily(
        self,
        *,
        name: str,
        hour_utc: int,
        minute_utc: int,
        job: Job,
    ) -> None:
        if not (0 <= hour_utc <= 23 and 0 <= minute_utc <= 59):
            raise ValueError(f"invalid time {hour_utc}:{minute_utc}")
        with self._lock:
            self._jobs.append(
                DailyAtJob(name=name, job=job, hour_utc=hour_utc, minute_utc=minute_utc)
            )

    def add_interval(self, *, name: str, interval_seconds: float, job: Job) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        with self._lock:
            self._jobs.append(
                IntervalJob(name=name, job=job, interval_seconds=interval_seconds)
            )

    def job_names(self) -> list[str]:
        with self._lock:
            return [j.name for j in self._jobs]

    # ── public: lifecycle ──

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            logger.debug("scheduler already running")
            return
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._run, name="notification-scheduler", daemon=False
        )
        thread.start()
        self._thread = thread

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning(
                "scheduler thread still alive after %.1fs; retaining reference (ADR-005)",
                timeout,
            )
            return
        self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── internal: worker loop ──

    def tick_once(self, *, now: Optional[datetime] = None) -> int:
        """Fire all due jobs once. Exposed for deterministic testing."""
        current = now if now is not None else self._clock()
        with self._lock:
            jobs = list(self._jobs)
        fired = 0
        for scheduled in jobs:
            if not scheduled.is_due(now=current):
                continue
            try:
                scheduled.job()
                fired += 1
            except Exception:  # noqa: BLE001 — isolate job failures
                logger.exception(
                    "scheduled job %s raised; continuing other jobs",
                    scheduled.name,
                )
            finally:
                scheduled.mark_fired(now=current)
        return fired

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick_once()
            except Exception:  # noqa: BLE001 — loop must not die
                logger.exception("scheduler loop iteration failed")
            self._stop_event.wait(self._tick_seconds)
