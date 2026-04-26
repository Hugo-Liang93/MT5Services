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
        # executor 自己的身份（不该被用作过滤键）
        runtime_identity=_RuntimeIdentity(instance_id="live:live-exec-a"),
        # 装配层注入的 main 身份（应作为过滤键）
        target_instance_id="live:live-main",
    )
    provider._runtime_rows()
    assert db.calls, "应至少调用一次 fetch_runtime_task_status"
    call = db.calls[0]
    assert call.get("instance_id") == "live:live-main", (
        f"必须按 target_instance_id（main 身份）过滤而不是 executor 身份；"
        f"got params={call!r}"
    )


def test_read_only_provider_without_target_instance_id_skips_instance_filter() -> None:
    """§0dh P2：未注入 target_instance_id 时不传 instance_id（仅按 role=main 查询，
    适用于 single_account 拓扑或诊断脚本——单 group 拓扑下 role=main 唯一命中）。
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
        target_instance_id=None,
    )
    provider._runtime_rows()
    call = db.calls[0]
    assert call.get("instance_id") is None, (
        f"无 target_instance_id 时不应传 instance_id；got params={call!r}"
    )


def test_read_only_provider_runtime_identity_not_used_as_filter_key() -> None:
    """§0dh P2 反向锁：即使传了 runtime_identity，也不应该被当作 instance_id 过滤键
    （那是 executor 自己的身份，按它查会永远命中 0 行）。
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
        runtime_identity=_RuntimeIdentity(instance_id="live:live-exec-a"),
        target_instance_id=None,  # 未注入 target → 不带 instance_id
    )
    provider._runtime_rows()
    call = db.calls[0]
    assert call.get("instance_id") != "live:live-exec-a", (
        f"runtime_identity.instance_id（executor 自己身份）不能被当作过滤键；"
        f"got params={call!r}"
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


# ── §0z P3 回归：identity-less 实例必须用唯一化 fallback 而非共享 'legacy' ──


def test_legacy_instance_id_returns_unique_per_process_fallback() -> None:
    """P3 §0z 回归：旧实现 identity-less 时显式写 'legacy' →
    所有 identity-less 本地脚本 / 测试实例 / 遗留进程共享同一组 runtime task
    记录互相覆盖。fallback 必须按 hostname + pid 唯一化，每个进程独立空间。
    """
    from src.config.runtime_identity import legacy_instance_id

    fallback = legacy_instance_id()
    assert fallback != "legacy", (
        f"identity-less fallback 不能再硬编码 'legacy'（共享 PK 空间）；got {fallback!r}"
    )
    # 必须可识别为 legacy 类（前缀 / 标签）便于审计
    assert "legacy" in fallback.lower(), (
        f"fallback 应保留 'legacy' 标签便于审计；got {fallback!r}"
    )
    # 同进程内多次调用必须稳定
    assert legacy_instance_id() == fallback, (
        "同进程多次调用必须返同一 fallback（PK 稳定性）"
    )
