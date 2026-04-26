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


# ── §0dh P2 回归：ReadOnlyEconomicCalendarProvider 必须按 main 的 target_instance_id 过滤 ──
# 旧 §0y P3 sentinel 锁了"按 runtime_identity.instance_id 过滤"——这是错误契约：
# executor 装 ReadOnly 时 runtime_identity 是 executor 自己的身份，按它过滤永远
# 命中 0 行（main 写入的 row 用的是 main 的 instance_id）。修正后契约：装配层
# 通过 topology 解析得到 same group 的 main instance_id，注入 target_instance_id。


def test_read_only_provider_runtime_rows_pass_target_instance_id() -> None:
    """§0dh P2：必须按外部注入的 target_instance_id（main 的 id）过滤，
    而不是 runtime_identity（executor 自己的 id）。
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
        # 装配层注入的 main 身份（必填）
        target_instance_id="live:live-main",
    )
    provider._runtime_rows()
    assert db.calls, "应至少调用一次 fetch_runtime_task_status"
    call = db.calls[0]
    assert call.get("instance_id") == "live:live-main", (
        f"必须按 target_instance_id（main 身份）过滤；got params={call!r}"
    )


def test_read_only_provider_target_instance_id_is_required() -> None:
    """§0dj：target_instance_id 必填，不接受 None / 空字符串兜底——装配层
    应通过 RuntimeIdentity.peer_main_instance_id 显式注入 main 身份。
    """
    import pytest

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
    with pytest.raises(ValueError, match="target_instance_id"):
        ReadOnlyEconomicCalendarProvider(
            db_writer=_RecordingDB(),
            settings=settings,
            target_instance_id="",
        )


# ── §0z P2 回归：stop_service 仅在 worker 真停下才能标 STOPPED ──


from src.calendar.economic_calendar.calendar_sync import stop_service
from src.monitoring.runtime_task_status import RuntimeTaskState


class _FakeAliveThread:
    """模拟 join(timeout) 后仍 is_alive()=True 的卡死线程。"""

    def __init__(self) -> None:
        self.joined_with_timeout: list[float | None] = []

    def is_alive(self) -> bool:
        return True

    def join(self, timeout: float | None = None) -> None:
        self.joined_with_timeout.append(timeout)


class _PersistRecorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, service: Any, job_type: str) -> None:
        self.calls.append(job_type)


def test_stop_service_does_not_mark_stopped_when_worker_still_alive(monkeypatch) -> None:
    """P2 §0z 回归：旧实现不管线程是否真停下，无条件把每个 job 持久化为 stopped
    → 把"线程卡死未退出"的高风险 shutdown 漂白成正常停止。线程真停下才能标
    STOPPED；卡死时必须保留线程引用 + 记录 STOP_REQUESTED 反映真实状态。
    """
    import src.calendar.economic_calendar.calendar_sync as cs_module

    persist_recorder = _PersistRecorder()
    monkeypatch.setattr(cs_module, "persist_job_state", persist_recorder)

    fake_thread = _FakeAliveThread()
    service = SimpleNamespace(
        _stop_event=SimpleNamespace(set=lambda: None),
        _worker=fake_thread,
        _job_state={
            "calendar_sync": {"last_status": "running"},
            "near_term_sync": {"last_status": "running"},
            "release_watch": {"last_status": "running"},
        },
    )

    stop_service(service)

    # worker 仍活着 → 不应被清掉引用（ADR-005）
    assert service._worker is fake_thread, (
        "worker join 超时仍 is_alive=True → 必须保留线程引用，不能清掉"
    )

    # 任一 job 不能被标 STOPPED（线程没真停下）
    for job_type, state in service._job_state.items():
        assert state["last_status"] != RuntimeTaskState.STOPPED.value, (
            f"线程卡死时 {job_type} 不能被标 STOPPED；got {state['last_status']!r}"
        )


def test_stop_service_marks_stopped_when_worker_actually_dies(monkeypatch) -> None:
    """对称契约：worker 真停下时正常标 STOPPED + 清引用。"""
    import src.calendar.economic_calendar.calendar_sync as cs_module

    persist_recorder = _PersistRecorder()
    monkeypatch.setattr(cs_module, "persist_job_state", persist_recorder)

    class _DeadThread:
        def is_alive(self) -> bool:
            return False

        def join(self, timeout: float | None = None) -> None:
            pass

    service = SimpleNamespace(
        _stop_event=SimpleNamespace(set=lambda: None),
        _worker=_DeadThread(),
        _job_state={
            "calendar_sync": {"last_status": "running"},
            "near_term_sync": {"last_status": "running"},
            "release_watch": {"last_status": "running"},
        },
    )

    stop_service(service)

    assert service._worker is None, "worker 真停下时应清引用"
    for job_type, state in service._job_state.items():
        assert state["last_status"] == RuntimeTaskState.STOPPED.value


# ── §0dj 反向锁：legacy_instance_id() 已移除，writer 必须显式 require RuntimeIdentity ──


def test_legacy_instance_id_helper_no_longer_exists() -> None:
    """§0dj：identity-less fallback 已彻底移除——所有 writer 必须显式接收
    RuntimeIdentity，无 identity 即装配失败 fail-fast，禁止任何 fallback 字面量。
    """
    import pytest
    from src.config import runtime_identity as ri_mod

    assert not hasattr(ri_mod, "legacy_instance_id"), (
        "legacy_instance_id() 已在 §0dj 移除（完整工程化清理：禁止 identity-less "
        "fallback 兜底）；如需 identity-less 调用，必须重新设计装配契约"
    )


def test_runtime_task_writer_raises_when_runtime_identity_missing() -> None:
    """§0dj：calendar_sync.runtime_task_row 必须在 _runtime_identity=None 时
    显式 raise，而不是 fallback 到任何字面量。
    """
    import pytest
    from src.calendar.economic_calendar.calendar_sync import runtime_task_row
    from types import SimpleNamespace

    service = SimpleNamespace(
        _runtime_identity=None,
        _job_state={"refresh": {"enabled": True}},
        _next_run_at={},
    )
    with pytest.raises(RuntimeError, match="runtime_identity"):
        runtime_task_row(service, "refresh")
