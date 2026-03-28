from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace

from src.calendar.economic_calendar import calendar_sync
from src.calendar.service import EconomicCalendarService


def test_job_interval_honors_zero_disable() -> None:
    service = EconomicCalendarService(
        db_writer=SimpleNamespace(),
        settings=SimpleNamespace(
            calendar_sync_interval_seconds=21600,
            near_term_refresh_interval_seconds=0,
            refresh_interval_seconds=900,
            release_watch_interval_seconds=120,
            local_timezone="UTC",
        ),
    )

    assert service._job_interval("calendar_sync") == 21600.0
    assert service._job_interval("near_term_sync") == 0.0
    assert service._job_interval("release_watch") == 120.0


def test_start_service_schedules_startup_jobs_without_blocking(monkeypatch) -> None:
    now = datetime(2026, 3, 28, tzinfo=timezone.utc)
    scheduled_calendar_sync = now + timedelta(seconds=180)
    persisted_jobs: list[str] = []
    thread_targets = []

    class _FakeThread:
        def __init__(self, *, target, name: str, daemon: bool) -> None:
            self._target = target
            self.name = name
            self.daemon = daemon
            self.started = False

        def start(self) -> None:
            self.started = True
            thread_targets.append(self.name)

        def is_alive(self) -> bool:
            return self.started

    service = SimpleNamespace(
        settings=SimpleNamespace(
            enabled=True,
            startup_refresh=True,
            calendar_sync_interval_seconds=21600,
            near_term_refresh_interval_seconds=0,
            release_watch_interval_seconds=120,
            startup_calendar_sync_delay_seconds=180,
            local_timezone="UTC",
        ),
        _worker=None,
        _stop_event=Event(),
        _next_run_at={job: None for job in calendar_sync._JOB_LABELS},
        _job_interval=lambda job_type: {
            "calendar_sync": 21600.0,
            "near_term_sync": 0.0,
            "release_watch": 120.0,
        }[job_type],
    )

    monkeypatch.setattr(calendar_sync, "_utc_now", lambda: now)
    monkeypatch.setattr(calendar_sync, "restore_job_state", lambda svc: None)
    monkeypatch.setattr(calendar_sync, "persist_job_state", lambda svc, job: persisted_jobs.append(job))
    monkeypatch.setattr(calendar_sync, "startup_schedule_time", lambda svc, job, current: scheduled_calendar_sync)
    monkeypatch.setattr(calendar_sync, "Thread", _FakeThread)
    monkeypatch.setattr(
        calendar_sync,
        "safe_run_job",
        lambda svc, job_type: (_ for _ in ()).throw(AssertionError("safe_run_job should not run during start_service")),
    )

    calendar_sync.start_service(service)

    assert service._next_run_at["near_term_sync"] is None
    assert service._next_run_at["release_watch"] == now
    assert service._next_run_at["calendar_sync"] == scheduled_calendar_sync
    assert persisted_jobs == ["calendar_sync", "near_term_sync", "release_watch"]
    assert thread_targets == ["economic-calendar-refresh"]
