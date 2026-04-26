from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.entrypoint.supervisor import ManagedProcess, Supervisor


class _DummyProcess:
    pid = 1234

    def poll(self):
        return None


def test_supervisor_start_initial_runs_main_gate_then_workers(monkeypatch) -> None:
    """main 与每个 worker 各做一次独立 gate 检查，main 必须先通过。"""
    calls: list[str] = []
    group = SimpleNamespace(
        name="live",
        main="live-main",
        workers=["live-exec-a", "live-exec-b"],
        environment="live",
    )
    supervisor = Supervisor(group, ready_timeout_seconds=5.0)

    monkeypatch.setattr(
        "src.entrypoint.supervisor.ensure_mt5_session_gate_or_raise",
        lambda instance_name=None: calls.append(f"gate:{instance_name}"),
    )
    monkeypatch.setattr(
        supervisor,
        "_spawn",
        lambda instance_name, restart_count=0: calls.append(f"spawn:{instance_name}") or ManagedProcess(
            instance_name=instance_name,
            process=_DummyProcess(),
            restart_count=restart_count,
        ),
    )
    monkeypatch.setattr(
        "src.entrypoint.supervisor._wait_for_ready",
        lambda instance_name, timeout_seconds: calls.append(f"ready:{instance_name}"),
    )

    supervisor._start_initial()

    # main gate → spawn main → wait ready → 每个 worker gate → spawn
    assert calls == [
        "gate:live-main",
        "spawn:live-main",
        "ready:live-main",
        "gate:live-exec-a",
        "spawn:live-exec-a",
        "gate:live-exec-b",
        "spawn:live-exec-b",
    ]


def test_supervisor_start_initial_main_gate_failure_aborts(monkeypatch) -> None:
    """main gate 失败必须 fail-fast，整个 group 启动中止。"""
    group = SimpleNamespace(
        name="live", main="live-main", workers=["live-exec-a"], environment="live",
    )
    supervisor = Supervisor(group, ready_timeout_seconds=5.0)

    def gate_fail(instance_name=None):
        raise RuntimeError("MT5 session gate failed for live-main [terminal_path_missing]")

    monkeypatch.setattr(
        "src.entrypoint.supervisor.ensure_mt5_session_gate_or_raise", gate_fail
    )

    with pytest.raises(RuntimeError, match="terminal_path_missing"):
        supervisor._start_initial()


def test_supervisor_start_initial_worker_gate_failure_skips_worker(monkeypatch) -> None:
    """worker gate 失败仅 warn 跳过该 worker，不阻断 main 启动。"""
    calls: list[str] = []
    group = SimpleNamespace(
        name="live",
        main="live-main",
        workers=["live-exec-a", "live-exec-b"],
        environment="live",
    )
    supervisor = Supervisor(group, ready_timeout_seconds=5.0)

    def gate(instance_name=None):
        if instance_name == "live-exec-a":
            raise RuntimeError("MT5 session gate failed for live-exec-a [terminal_not_running]")
        calls.append(f"gate:{instance_name}")

    monkeypatch.setattr(
        "src.entrypoint.supervisor.ensure_mt5_session_gate_or_raise", gate
    )
    monkeypatch.setattr(
        supervisor,
        "_spawn",
        lambda instance_name, restart_count=0: calls.append(f"spawn:{instance_name}") or ManagedProcess(
            instance_name=instance_name,
            process=_DummyProcess(),
            restart_count=restart_count,
        ),
    )
    monkeypatch.setattr(
        "src.entrypoint.supervisor._wait_for_ready",
        lambda instance_name, timeout_seconds: calls.append(f"ready:{instance_name}"),
    )

    supervisor._start_initial()

    # main 正常启动；live-exec-a gate 失败被跳过；live-exec-b 仍尝试 + 启动
    assert calls == [
        "gate:live-main",
        "spawn:live-main",
        "ready:live-main",
        "gate:live-exec-b",
        "spawn:live-exec-b",
    ]
    # 跳过的 worker 不在 _managed
    assert "live-exec-a" not in supervisor._managed
    assert "live-main" in supervisor._managed
    assert "live-exec-b" in supervisor._managed


def test_supervisor_spawn_no_longer_runs_gate(monkeypatch) -> None:
    """_spawn 不应再调用 gate（gate 由调用方负责）。"""
    group = SimpleNamespace(
        name="live", main="live-main", workers=[], environment="live",
    )
    supervisor = Supervisor(group, ready_timeout_seconds=5.0)
    gate_calls: list[str] = []

    monkeypatch.setattr(
        "src.entrypoint.supervisor.ensure_mt5_session_gate_or_raise",
        lambda instance_name=None: gate_calls.append(instance_name),
    )
    monkeypatch.setattr(
        "subprocess.Popen", lambda *args, **kwargs: _DummyProcess(),
    )

    managed = supervisor._spawn("live-main")

    assert managed.instance_name == "live-main"
    assert gate_calls == []  # _spawn 内部不再触发 gate


def test_supervisor_pending_worker_retry_spawns_once_gate_passes(monkeypatch) -> None:
    """启动期 gate 失败的 worker 应被放入 _pending_workers；gate 恢复后自动 spawn。"""
    group = SimpleNamespace(
        name="live", main="live-main", workers=["live-exec-a"], environment="live",
    )
    supervisor = Supervisor(group, ready_timeout_seconds=5.0)
    supervisor._pending_workers.add("live-exec-a")
    # 跳过节流，让重试立即生效
    supervisor._last_pending_retry_at = 0.0
    supervisor._PENDING_WORKER_RETRY_INTERVAL_SECONDS = 0.0

    gate_calls: list[str] = []

    def gate(instance_name=None):
        gate_calls.append(instance_name)
        # 首次仍失败，第二次起通过
        if len(gate_calls) == 1:
            raise RuntimeError("terminal_not_running")

    monkeypatch.setattr(
        "src.entrypoint.supervisor.ensure_mt5_session_gate_or_raise", gate
    )
    spawn_calls: list[str] = []
    monkeypatch.setattr(
        supervisor,
        "_spawn",
        lambda instance_name, restart_count=0: spawn_calls.append(instance_name)
        or ManagedProcess(
            instance_name=instance_name,
            process=_DummyProcess(),
            restart_count=restart_count,
        ),
    )

    # 第一次 retry: gate 仍失败 → pending 保留
    supervisor._retry_pending_workers()
    assert "live-exec-a" in supervisor._pending_workers
    assert spawn_calls == []

    # 第二次 retry: gate 通过 → spawn + 从 pending 移除
    supervisor._last_pending_retry_at = 0.0  # 再次绕过节流
    supervisor._retry_pending_workers()
    assert "live-exec-a" not in supervisor._pending_workers
    assert spawn_calls == ["live-exec-a"]
    assert "live-exec-a" in supervisor._managed


def test_supervisor_pending_worker_retry_respects_throttle(monkeypatch) -> None:
    """节流：在同一 interval 内不应重复调 gate。"""
    group = SimpleNamespace(
        name="live", main="live-main", workers=["live-exec-a"], environment="live",
    )
    supervisor = Supervisor(group, ready_timeout_seconds=5.0)
    supervisor._pending_workers.add("live-exec-a")
    supervisor._PENDING_WORKER_RETRY_INTERVAL_SECONDS = 30.0
    # 设为当前时间 - 1s，还没到 30s 节流间隔
    import time as _time
    supervisor._last_pending_retry_at = _time.monotonic() - 1.0

    gate_calls: list[str] = []
    monkeypatch.setattr(
        "src.entrypoint.supervisor.ensure_mt5_session_gate_or_raise",
        lambda instance_name=None: gate_calls.append(instance_name),
    )

    supervisor._retry_pending_workers()
    assert gate_calls == []  # 节流生效，未触发 gate


# ── §0aa P1 回归：normal 退出（exit code 0）也必须重启或主动出列 ──


class _ExitedNormallyProcess:
    """模拟 exit code = 0 的子进程。"""
    pid = 9999

    def poll(self):
        return 0


def test_monitor_loop_does_not_keep_stale_normal_exit_in_managed(monkeypatch) -> None:
    """P1 §0aa 回归：旧实现对 normal exit 仅 log + continue，_managed 中保留
    已退出的实例 → 主实例可悄悄消失而 supervisor 不会拉起；worker 静默缺员。
    必须把 normal exit 当成 unexpected 退出处理（重启 + 计数），与 warning/fatal
    分类一致；或至少把退出实例从 _managed 移除以暴露真实拓扑。
    """
    group = SimpleNamespace(
        name="live",
        main="live-main",
        workers=[],
        environment="live",
    )
    supervisor = Supervisor(group, ready_timeout_seconds=5.0)
    supervisor._managed["live-main"] = ManagedProcess(
        instance_name="live-main",
        process=_ExitedNormallyProcess(),
        restart_count=0,
    )

    spawned: list[str] = []
    monkeypatch.setattr(
        "src.entrypoint.supervisor.ensure_mt5_session_gate_or_raise",
        lambda instance_name=None: None,
    )
    monkeypatch.setattr(
        "src.entrypoint.supervisor._wait_for_ready",
        lambda instance_name, timeout_seconds: None,
    )
    monkeypatch.setattr(
        "src.entrypoint.supervisor.time.sleep",
        lambda *_args, **_kwargs: setattr(supervisor, "_stopping", True),
    )
    monkeypatch.setattr(
        supervisor,
        "_spawn",
        lambda instance_name, restart_count=0: spawned.append(instance_name) or ManagedProcess(
            instance_name=instance_name,
            process=_DummyProcess(),
            restart_count=restart_count,
        ),
    )

    supervisor._monitor_loop()

    # 验证：(a) _managed 不能仍持有 _ExitedNormallyProcess 引用 OR (b) 已重启
    current = supervisor._managed.get("live-main")
    if current is None:
        # 路径 (b)：被移除即可（拓扑暴露真实状态）
        return
    assert isinstance(current.process, _DummyProcess) and not isinstance(
        current.process, _ExitedNormallyProcess
    ), (
        "normal exit 后 _managed 不能仍持有已 exited 的 process 引用；"
        "必须重启或移除以暴露拓扑真相。"
        f"got managed.process={current.process!r}, spawned={spawned!r}"
    )
