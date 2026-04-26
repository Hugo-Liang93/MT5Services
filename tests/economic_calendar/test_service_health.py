from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.calendar.service import EconomicCalendarService
from src.config import EconomicConfig
from src.monitoring.health.checks import check_economic_calendar
from src.monitoring.runtime_task_status import RuntimeTaskState




# §0dk P2：EconomicCalendarService 必填 runtime_identity；test 用 SimpleNamespace stub。
def _calendar_runtime_identity():
    from types import SimpleNamespace
    return SimpleNamespace(
        instance_id="live:test-main",
        instance_role="main",
        account_key="live:Broker-Test:1001",
        account_alias="main",
    )

class _DummyDB:
    def __init__(self, rows=None) -> None:
        self._rows = list(rows or [])
        self.runtime_rows = []

    def fetch_runtime_task_status(
        self,
        component=None,
        task_name=None,
        instance_id=None,
        instance_role=None,
        account_key=None,
        account_alias=None,
    ):
        return list(self._rows)

    def write_runtime_task_status(self, rows):
        self.runtime_rows.extend(rows)


class _DummyThread:
    def __init__(self, alive: bool = True) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


class _DummyMonitor:
    def __init__(self, now: datetime) -> None:
        self._now = now
        self.metrics = []

    def _utc_now(self) -> datetime:
        return self._now

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def record_metric(self, component: str, name: str, value: float, metadata):
        self.metrics.append((component, name, value, metadata))


def _runtime_task_row(
    *,
    task_name: str,
    state: str,
    started_at: datetime,
    completed_at: datetime,
    duration_ms: int,
    last_error: str | None = None,
    consecutive_failures: int = 0,
    success_count: int | None = None,
    last_result_status: str | None = None,
):
    return (
        "economic_calendar",
        task_name,
        completed_at,
        state,
        started_at,
        completed_at,
        None,
        duration_ms,
        success_count
        if success_count is not None
        else (1 if state in {RuntimeTaskState.OK.value, RuntimeTaskState.PARTIAL.value} else 0),
        0 if state in {RuntimeTaskState.OK.value, RuntimeTaskState.PARTIAL.value} else 1,
        consecutive_failures,
        last_error,
        {
            "last_result_status": last_result_status or state,
            "last_fetched": 1,
            "last_written": 1,
            "last_snapshots": 1,
        },
    )


def test_service_does_not_report_stale_while_bootstrap_window_is_open(monkeypatch) -> None:
    now = datetime(2026, 4, 12, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("src.calendar.service._utc_now", lambda: now)

    service = EconomicCalendarService(_DummyDB(), settings=EconomicConfig(), runtime_identity=_calendar_runtime_identity())
    service._scheduler_started_at = now - timedelta(seconds=30)
    service._bootstrap_deadline_at = now + timedelta(seconds=180)
    service._worker = _DummyThread(alive=True)

    assert service.is_warming_up() is True
    assert service.is_stale() is False
    assert service.staleness_seconds() is None
    assert service.health_state() == "warming_up"


def test_service_reports_stale_after_bootstrap_deadline_without_success(monkeypatch) -> None:
    now = datetime(2026, 4, 12, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("src.calendar.service._utc_now", lambda: now)

    service = EconomicCalendarService(_DummyDB(), settings=EconomicConfig(), runtime_identity=_calendar_runtime_identity())
    service._scheduler_started_at = now - timedelta(minutes=10)
    service._bootstrap_deadline_at = now - timedelta(seconds=1)
    service._worker = _DummyThread(alive=True)

    assert service.is_warming_up() is False
    assert service.is_stale() is True
    assert math.isinf(service.staleness_seconds())
    assert service.health_state() == "stale"


def test_restore_job_state_recovers_last_successful_refresh_timestamp() -> None:
    now = datetime(2026, 4, 12, 15, 0, tzinfo=timezone.utc)
    last_success_completed = now - timedelta(minutes=2)
    latest_error_completed = now - timedelta(minutes=1)
    db = _DummyDB(
        [
            _runtime_task_row(
                task_name="calendar_sync",
                state=RuntimeTaskState.OK.value,
                started_at=last_success_completed - timedelta(seconds=12),
                completed_at=last_success_completed,
                duration_ms=1200,
            ),
            _runtime_task_row(
                task_name="release_watch",
                state=RuntimeTaskState.ERROR.value,
                started_at=latest_error_completed - timedelta(seconds=5),
                completed_at=latest_error_completed,
                duration_ms=500,
                last_error="timeout",
                consecutive_failures=1,
            ),
        ]
    )
    service = EconomicCalendarService(db, settings=EconomicConfig(), runtime_identity=_calendar_runtime_identity())

    service._restore_job_state()

    assert service._last_refresh_at == last_success_completed
    assert service._last_refresh_completed_at == latest_error_completed
    assert service._last_refresh_error == "timeout"
    assert service._last_refresh_summary == {
        "job_type": "release_watch",
        "status": RuntimeTaskState.ERROR.value,
    }


def test_restore_job_state_recovers_last_success_after_stop() -> None:
    now = datetime(2026, 4, 12, 15, 0, tzinfo=timezone.utc)
    last_success_completed = now - timedelta(minutes=2)
    db = _DummyDB(
        [
            _runtime_task_row(
                task_name="calendar_sync",
                state=RuntimeTaskState.STOPPED.value,
                started_at=last_success_completed - timedelta(seconds=12),
                completed_at=last_success_completed,
                duration_ms=1200,
                success_count=3,
                last_result_status=RuntimeTaskState.OK.value,
            ),
        ]
    )
    service = EconomicCalendarService(db, settings=EconomicConfig(), runtime_identity=_calendar_runtime_identity())

    service._restore_job_state()

    assert service._last_refresh_at == last_success_completed
    assert service._last_refresh_summary == {
        "job_type": "calendar_sync",
        "status": RuntimeTaskState.OK.value,
    }


def test_persist_job_state_emits_instance_aware_runtime_row() -> None:
    db = _DummyDB()
    runtime_identity = SimpleNamespace(
        instance_id="main-live-main-abc123",
        instance_role="main",
        account_key="live:broker-live:1001",
        account_alias="live_main",
    )
    service = EconomicCalendarService(
        db,
        settings=EconomicConfig(),
        runtime_identity=runtime_identity,
    )

    service._persist_job_state("calendar_sync")

    assert len(db.runtime_rows) == 1
    row = db.runtime_rows[0]
    assert len(row) == 17
    assert row[13] == runtime_identity.instance_id
    assert row[14] == runtime_identity.instance_role
    assert row[15] == runtime_identity.account_key
    assert row[16] == runtime_identity.account_alias


def test_health_check_uses_zero_staleness_during_warmup() -> None:
    now = datetime(2026, 4, 12, 15, 0, tzinfo=timezone.utc)
    monitor = _DummyMonitor(now)

    class _DummyService:
        def stats(self):
            return {
                "warming_up": "true",
                "staleness_seconds": None,
                "provider_status": {},
            }

    stats = check_economic_calendar(monitor, "economic_calendar", _DummyService())

    assert stats["warming_up"] == "true"
    staleness_metric = next(item for item in monitor.metrics if item[1] == "economic_calendar_staleness")
    assert staleness_metric[2] == 0.0
