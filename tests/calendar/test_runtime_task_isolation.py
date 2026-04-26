"""§0y P2/P3 回归：calendar_sync.restore_job_state + ReadOnlyEconomicCalendarProvider
查询 runtime_task_status 时必须按 instance_id 隔离，禁止跨实例串行/读取
其他实例的 next_run_at / failure_count / freshness。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from src.calendar.economic_calendar.calendar_sync import restore_job_state
from src.calendar.read_only_provider import ReadOnlyEconomicCalendarProvider


# ── 共用 stub ──


class _RuntimeIdentity:
    def __init__(
        self,
        instance_id: str = "instance-a",
        instance_role: str = "main",
        account_key: str = "live:srv:1",
    ) -> None:
        self.instance_id = instance_id
        self.instance_role = instance_role
        self.account_key = account_key


class _RecordingDB:
    """记录 fetch_runtime_task_status 调用参数；返回空行。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def fetch_runtime_task_status(self, **kwargs: Any) -> list:
        self.calls.append(dict(kwargs))
        return []


# ── §0y P2 回归：calendar_sync.restore_job_state 必须按 instance_id 过滤 ──


def test_restore_job_state_passes_instance_id_to_runtime_task_query() -> None:
    """旧实现只按 component + instance_role + account_key 取行，没带 instance_id
    → 滚动重启 / 双开实例 / 同账户重复进程下，会把别人的 next_run_at + 失败计数
    串行恢复到本实例。
    """
    db = _RecordingDB()
    service = SimpleNamespace(
        db=db,
        _runtime_identity=_RuntimeIdentity(instance_id="instance-a"),
        _job_state={},
        _last_refresh_at=None,
    )
    restore_job_state(service)
    assert db.calls, "应至少调用一次 fetch_runtime_task_status"
    call = db.calls[0]
    assert call.get("instance_id") == "instance-a", (
        f"必须按 instance_id 过滤；got params={call!r}"
    )


# ── §0y P3 回归：ReadOnlyEconomicCalendarProvider 必须按 instance_id 过滤 ──


def test_read_only_provider_runtime_rows_pass_instance_id() -> None:
    """旧 _runtime_rows 只按 component='economic_calendar' + instance_role='main'
    查询 → 多 main 实例同库时 freshness/error/provider_status 可能来自别的实例，
    而不是当前 executor 应该依赖的那一个。
    """
    db = _RecordingDB()
    settings = SimpleNamespace(
        enabled=True,
        stale_after_seconds=3600,
        trade_guard_provider_failure_threshold=3,
        high_importance_threshold=2,
        local_timezone="UTC",
        near_term_refresh_interval_seconds=60,
        calendar_sync_interval_seconds=300,
        release_watch_interval_seconds=30,
        near_term_window_hours=12,
        release_watch_lookback_minutes=5,
        release_watch_lookahead_minutes=15,
        default_countries=("US",),
        curated_sources=None,
        curated_countries=None,
        curated_currencies=None,
        curated_statuses=None,
        curated_importance_min=2,
        curated_include_all_day=True,
    )
    provider = ReadOnlyEconomicCalendarProvider(
        db_writer=db,
        settings=settings,
        runtime_identity=_RuntimeIdentity(instance_id="executor-a"),
    )
    provider._runtime_rows()
    assert db.calls, "应至少调用一次 fetch_runtime_task_status"
    call = db.calls[0]
    assert call.get("instance_id") == "executor-a", (
        f"必须按 instance_id 过滤；got params={call!r}"
    )


def test_read_only_provider_without_runtime_identity_skips_instance_filter() -> None:
    """对称契约：runtime_identity=None 时不传 instance_id（保留旧全局查询行为，
    避免破坏没注入身份的旧调用方）。
    """
    db = _RecordingDB()
    settings = SimpleNamespace(
        enabled=True,
        stale_after_seconds=3600,
        trade_guard_provider_failure_threshold=3,
        high_importance_threshold=2,
        local_timezone="UTC",
        near_term_refresh_interval_seconds=60,
        calendar_sync_interval_seconds=300,
        release_watch_interval_seconds=30,
        near_term_window_hours=12,
        release_watch_lookback_minutes=5,
        release_watch_lookahead_minutes=15,
        default_countries=("US",),
        curated_sources=None,
        curated_countries=None,
        curated_currencies=None,
        curated_statuses=None,
        curated_importance_min=2,
        curated_include_all_day=True,
    )
    provider = ReadOnlyEconomicCalendarProvider(
        db_writer=db,
        settings=settings,
        runtime_identity=None,
    )
    provider._runtime_rows()
    call = db.calls[0]
    assert call.get("instance_id") is None, (
        f"无 runtime_identity 时不应传 instance_id；got params={call!r}"
    )
