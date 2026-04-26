"""OperatorCommandConsumer 线程生命周期 + 异常恢复 + 命令分支补全测试。

补全 tests/trading/test_operator_commands.py 的覆盖盲区：
- start/stop/restart 线程行为（含 idempotency / 中途打断）
- _process_command 在执行抛异常时的 fail 分支
- _execute_command 各种依赖缺失场景（runtime_mode_controller / trade_executor /
  exposure_closeout_controller / pending_entry_manager 缺失）
- 未知 command_type 报错
- heartbeat_fn 调用契约
- 复合命令分支（set_trade_control + reset_circuit / runtime_mode partial_failure）
"""
from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.config.runtime_identity import RuntimeIdentity, build_account_key
from src.monitoring.pipeline import (
    PIPELINE_COMMAND_COMPLETED,
    PIPELINE_COMMAND_FAILED,
    PipelineEventBus,
)
from src.trading.commands.consumer import OperatorCommandConsumer


def _runtime_identity() -> RuntimeIdentity:
    return RuntimeIdentity(
        instance_name="live-exec-a",
        environment="live",
        instance_id="executor-live-exec-a",
        instance_role="executor",
        live_topology_mode="multi_account",
        account_alias="exec_a",
        account_label="Exec A",
        account_key=build_account_key("live", "Broker-Live", 1002),
        mt5_server="Broker-Live",
        mt5_login=1002,
        mt5_path="C:/MT5/exec_a/terminal64.exe",
        peer_main_instance_id="live:live-main",
    )


def _make_consumer(
    *,
    claim_fn=None,
    complete_fn=None,
    heartbeat_fn=None,
    command_service=None,
    runtime_mode_controller=None,
    trade_executor=None,
    exposure_closeout_controller=None,
    pending_entry_manager=None,
    pipeline_event_bus=None,
    poll_interval_seconds: float = 0.1,
) -> OperatorCommandConsumer:
    return OperatorCommandConsumer(
        claim_fn=claim_fn or (lambda **kwargs: []),
        complete_fn=complete_fn or (lambda **kwargs: None),
        heartbeat_fn=heartbeat_fn,
        runtime_identity=_runtime_identity(),
        command_service=command_service or MagicMock(),
        runtime_mode_controller=runtime_mode_controller,
        exposure_closeout_controller=exposure_closeout_controller,
        pending_entry_manager=pending_entry_manager,
        trade_executor=trade_executor,
        pipeline_event_bus=pipeline_event_bus,
        poll_interval_seconds=poll_interval_seconds,
    )


# ── 线程生命周期 ────────────────────────────────────────────────────────


def test_start_spawns_worker_thread() -> None:
    """start 后 worker 线程立即处于 running 状态。"""
    consumer = _make_consumer()
    try:
        consumer.start()
        # 等线程真正起来
        for _ in range(20):
            if consumer.is_running():
                break
            time.sleep(0.01)
        assert consumer.is_running() is True
    finally:
        consumer.stop(timeout=2.0)


def test_start_is_idempotent_when_already_running() -> None:
    """重复调 start 不应再 spawn 新线程（OwnedThreadLifecycle 保护）。"""
    consumer = _make_consumer()
    try:
        consumer.start()
        first_thread = consumer._worker_thread
        consumer.start()  # 应该跳过
        assert consumer._worker_thread is first_thread
    finally:
        consumer.stop(timeout=2.0)


def test_stop_terminates_worker_cleanly() -> None:
    """stop 置位 stop_event → worker loop 检测后退出 → is_running False。"""
    consumer = _make_consumer()
    consumer.start()
    consumer.stop(timeout=2.0)
    assert consumer.is_running() is False


def test_stop_is_safe_when_never_started() -> None:
    """从未 start 直接 stop 不应抛异常。"""
    consumer = _make_consumer()
    consumer.stop(timeout=0.5)  # 不抛 = 通过


# ── _process_command 异常恢复 ───────────────────────────────────────────


def test_process_command_emits_failed_on_execute_exception(monkeypatch) -> None:
    """_execute_command 抛异常 → complete_fn(status='failed') + emit command_failed。"""
    completed: list[dict] = []
    received = []
    pipeline_bus = PipelineEventBus()
    pipeline_bus.add_listener(received.append)

    cmd_service = MagicMock()
    cmd_service.update_trade_control.side_effect = RuntimeError("boom")

    consumer = _make_consumer(
        complete_fn=lambda **kwargs: completed.append(kwargs),
        command_service=cmd_service,
        pipeline_event_bus=pipeline_bus,
    )
    consumer._process_command(
        {
            "command_id": "fail-1",
            "command_type": "set_trade_control",
            "action_id": "act-fail-1",
            "actor": "tester",
            "payload": {"trace_id": "trace-fail-1"},
        }
    )

    assert len(completed) == 1
    assert completed[0]["status"] == "failed"
    assert completed[0]["last_error_code"] == "RuntimeError"
    failed_events = [e for e in received if e.type == PIPELINE_COMMAND_FAILED]
    assert len(failed_events) == 1


def test_process_command_runs_risk_projection_after_completion() -> None:
    """正常完成后 _project_risk_state 也调（让 risk state 立刻刷新）。"""
    projector = MagicMock()
    projector.project_now = MagicMock()
    cmd_service = MagicMock()
    cmd_service.update_trade_control.return_value = {"auto_entry_enabled": True}
    cmd_service.record_operator_action.return_value = {"operation_id": "audit-x"}

    consumer = OperatorCommandConsumer(
        claim_fn=lambda **kwargs: [],
        complete_fn=lambda **kwargs: None,
        runtime_identity=_runtime_identity(),
        command_service=cmd_service,
        account_risk_state_projector=projector,
    )
    consumer._process_command(
        {
            "command_id": "ok-1",
            "command_type": "set_trade_control",
            "action_id": "act-ok-1",
            "actor": "tester",
            "payload": {"auto_entry_enabled": True},
        }
    )
    assert projector.project_now.called


# ── _execute_command 依赖缺失 ───────────────────────────────────────────


def test_execute_runtime_mode_without_controller_raises() -> None:
    """set_runtime_mode 时 runtime_mode_controller=None → RuntimeError。"""
    consumer = _make_consumer(runtime_mode_controller=None)
    with pytest.raises(RuntimeError, match="runtime_mode_controller_not_configured"):
        consumer._execute_command(
            {
                "command_id": "rm-1",
                "command_type": "set_runtime_mode",
                "action_id": "act-rm-1",
                "actor": "tester",
                "payload": {"mode": "ingest_only"},
            }
        )


def test_execute_reset_circuit_without_executor_raises() -> None:
    """reset_circuit_breaker 时 trade_executor=None → RuntimeError。"""
    consumer = _make_consumer(trade_executor=None)
    with pytest.raises(RuntimeError, match="trade_executor_not_configured"):
        consumer._execute_command(
            {
                "command_id": "rc-1",
                "command_type": "reset_circuit_breaker",
                "action_id": "act-rc-1",
                "actor": "tester",
                "payload": {},
            }
        )


def test_execute_close_exposure_without_controller_raises() -> None:
    """close_exposure 时 exposure_closeout_controller=None → RuntimeError。"""
    consumer = _make_consumer(exposure_closeout_controller=None)
    with pytest.raises(RuntimeError, match="exposure_closeout_controller_not_configured"):
        consumer._execute_command(
            {
                "command_id": "ce-1",
                "command_type": "close_exposure",
                "action_id": "act-ce-1",
                "actor": "tester",
                "payload": {},
            }
        )


def test_execute_cancel_pending_without_manager_raises() -> None:
    """cancel_pending_entry 时 pending_entry_manager=None → RuntimeError。"""
    consumer = _make_consumer(pending_entry_manager=None)
    with pytest.raises(RuntimeError, match="pending_entry_manager_not_configured"):
        consumer._execute_command(
            {
                "command_id": "cp-1",
                "command_type": "cancel_pending_entry",
                "action_id": "act-cp-1",
                "actor": "tester",
                "payload": {"signal_id": "sig-1"},
            }
        )


def test_execute_unknown_command_raises_value_error() -> None:
    """未知 command_type → ValueError。"""
    consumer = _make_consumer()
    with pytest.raises(ValueError, match="unsupported operator command"):
        consumer._execute_command(
            {
                "command_id": "unk-1",
                "command_type": "no_such_command_type",
                "action_id": "act-unk-1",
                "actor": "tester",
                "payload": {},
            }
        )


# ── 复合命令路径 ────────────────────────────────────────────────────────


def test_set_trade_control_reset_circuit_invokes_executor() -> None:
    """set_trade_control 带 reset_circuit=True 时调 trade_executor.reset_circuit。"""
    cmd_service = MagicMock()
    cmd_service.update_trade_control.return_value = {"auto_entry_enabled": False}
    cmd_service.record_operator_action.return_value = {"operation_id": "audit-y"}

    executor = MagicMock()
    consumer = _make_consumer(command_service=cmd_service, trade_executor=executor)

    consumer._execute_command(
        {
            "command_id": "tc-1",
            "command_type": "set_trade_control",
            "action_id": "act-tc-1",
            "actor": "tester",
            "payload": {"auto_entry_enabled": False, "reset_circuit": True},
        }
    )
    executor.reset_circuit.assert_called_once()


def test_set_runtime_mode_partial_failure_propagates_error_status() -> None:
    """runtime_mode 切换返回 last_error → status='partial_failure' + 错误消息。"""
    controller = MagicMock()
    controller.apply_mode.return_value = {
        "current_mode": "ingest_only",
        "last_error": "trade_executor_stop_failed",
    }
    cmd_service = MagicMock()
    cmd_service.record_operator_action.return_value = {"operation_id": "audit-rm"}

    consumer = _make_consumer(
        command_service=cmd_service, runtime_mode_controller=controller
    )

    response = consumer._execute_command(
        {
            "command_id": "rm-2",
            "command_type": "set_runtime_mode",
            "action_id": "act-rm-2",
            "actor": "tester",
            "payload": {"mode": "ingest_only"},
        }
    )
    assert response["status"] == "partial_failure"
    assert "trade_executor_stop_failed" in response["message"]


def test_reset_circuit_breaker_returns_circuit_state_snapshot() -> None:
    """reset_circuit_breaker 返回 effective_state.circuit_breaker 含 open/failures/timestamp。"""
    cmd_service = MagicMock()
    cmd_service.record_operator_action.return_value = {"operation_id": "audit-rc"}
    executor = MagicMock()
    executor.circuit_open = False
    executor.consecutive_failures = 0
    executor.circuit_open_at = None

    consumer = _make_consumer(command_service=cmd_service, trade_executor=executor)
    response = consumer._execute_command(
        {
            "command_id": "rc-2",
            "command_type": "reset_circuit_breaker",
            "action_id": "act-rc-2",
            "actor": "tester",
            "payload": {},
        }
    )
    executor.reset_circuit.assert_called_once()
    cb_state = response["effective_state"]["circuit_breaker"]
    assert cb_state["open"] is False
    assert cb_state["consecutive_failures"] == 0
    assert cb_state["circuit_open_at"] is None


# ── heartbeat ─────────────────────────────────────────────────────────


def test_heartbeat_fn_invoked_with_runtime_identity_and_lease() -> None:
    """_process_command 内部应调 heartbeat_fn 延 lease，参数与 runtime_identity 对齐。"""
    heartbeat_calls: list[dict] = []

    def _heartbeat(**kwargs):
        heartbeat_calls.append(kwargs)

    cmd_service = MagicMock()
    cmd_service.update_trade_control.return_value = {"auto_entry_enabled": True}
    cmd_service.record_operator_action.return_value = {"operation_id": "audit-h"}

    consumer = _make_consumer(
        command_service=cmd_service, heartbeat_fn=_heartbeat
    )
    consumer._lease_seconds = 30  # 已在 init 中默认，这里显式标记

    consumer._process_command(
        {
            "command_id": "hb-1",
            "command_type": "set_trade_control",
            "action_id": "act-hb-1",
            "actor": "tester",
            "payload": {"auto_entry_enabled": True},
        }
    )

    assert len(heartbeat_calls) == 1
    call = heartbeat_calls[0]
    assert call["command_id"] == "hb-1"
    assert call["claimed_by_instance_id"] == "executor-live-exec-a"
    assert call["lease_seconds"] == 30


def test_heartbeat_optional_when_function_missing() -> None:
    """heartbeat_fn=None 时 _process_command 不应抛。"""
    cmd_service = MagicMock()
    cmd_service.update_trade_control.return_value = {"auto_entry_enabled": True}
    cmd_service.record_operator_action.return_value = {"operation_id": "audit-h2"}

    consumer = _make_consumer(command_service=cmd_service, heartbeat_fn=None)
    consumer._process_command(
        {
            "command_id": "hb-2",
            "command_type": "set_trade_control",
            "action_id": "act-hb-2",
            "actor": "tester",
            "payload": {"auto_entry_enabled": True},
        }
    )  # 不抛 = 通过


# ── _worker 中途 stop ───────────────────────────────────────────────────


def test_worker_stops_mid_batch_when_stop_event_set(monkeypatch) -> None:
    """_worker 处理 batch 中途若 stop_event 被 set，立即 return 不再处理后续 commands。"""
    cmd_service = MagicMock()
    cmd_service.update_trade_control.return_value = {"auto_entry_enabled": True}
    cmd_service.record_operator_action.return_value = {"operation_id": "audit-mb"}
    processed: list[str] = []

    consumer = _make_consumer(command_service=cmd_service)

    def _process_with_stop(item):
        processed.append(item["command_id"])
        # 处理完第一个就 set stop_event
        consumer._stop_event.set()

    monkeypatch.setattr(consumer, "_process_command", _process_with_stop)
    monkeypatch.setattr(
        "src.trading.commands.consumer.time.sleep", lambda *_a, **_k: None
    )

    def _claim_fn(**kwargs):
        # 一次返回 3 条；第一次进 worker 后第一条处理完 stop_event 就被 set
        return {
            "claimed": [
                {"command_id": f"cmd-{i}", "command_type": "set_trade_control",
                 "payload": {"trace_id": f"t-{i}"}}
                for i in range(3)
            ],
            "dead_lettered": [],
        }

    consumer._claim_fn = _claim_fn
    consumer._worker()

    # 处理了第 1 条后 stop_event 触发 → 仅 cmd-0 被处理，cmd-1 / cmd-2 被中断
    assert processed == ["cmd-0"]


# ── §0cc P1 回归：claim_fn / complete_fn 一次失败不能打死 worker 线程 ──


def test_consumer_survives_transient_claim_fn_exception() -> None:
    """P1 §0cc 回归：旧实现 _worker 主循环没有顶层 try/except，claim_fn
    抛一次瞬时异常 → 线程直接退出，整个控制面 silently down。
    必须把 claim 异常隔离在循环内，sleep + 下次 retry。
    """
    call_count = [0]

    def flaky_claim(**kwargs: Any) -> Any:
        call_count[0] += 1
        if call_count[0] < 3:
            raise RuntimeError("db down")
        return []  # 第 3 次起返回正常

    consumer = _make_consumer(
        claim_fn=flaky_claim,
        poll_interval_seconds=0.05,
    )
    try:
        consumer.start()
        # 等多个 poll 周期：第 1/2 次 raise，第 3 次起恢复
        for _ in range(20):
            time.sleep(0.05)
            if call_count[0] >= 3:
                break
        assert consumer.is_running(), (
            f"claim_fn 瞬时异常不能打死 worker 线程；call_count={call_count[0]}"
        )
        assert call_count[0] >= 3, (
            f"线程必须 retry，至少 3 次调用；got {call_count[0]}"
        )
    finally:
        consumer.stop(timeout=2.0)


def test_consumer_survives_transient_complete_fn_exception() -> None:
    """P1 §0cc 回归：_process_command 失败分支再次调 _complete_fn 没有第二层
    保护，complete_fn 一次抛异常就把 worker 线程打死。必须把 complete_fn
    所有调用包 try/except，让 consumer 在瞬时 DB 故障下持续运行。
    """
    claimed_items = [
        {
            "command_id": "cmd-1",
            "command_type": "set_runtime_mode",
            "request_payload": {"mode": "full"},
            "attempt_count": 1,
        }
    ]
    call_count = [0]
    complete_calls = [0]

    def claim_once(**kwargs: Any) -> Any:
        call_count[0] += 1
        if call_count[0] == 1:
            return claimed_items
        return []

    def flaky_complete(**kwargs: Any) -> None:
        complete_calls[0] += 1
        raise RuntimeError("complete down")

    consumer = _make_consumer(
        claim_fn=claim_once,
        complete_fn=flaky_complete,
        poll_interval_seconds=0.05,
    )
    try:
        consumer.start()
        # 等多个 poll 周期
        for _ in range(20):
            time.sleep(0.05)
            if call_count[0] >= 3:
                break
        assert consumer.is_running(), (
            f"complete_fn 瞬时异常不能打死 worker 线程；"
            f"claim_calls={call_count[0]} complete_calls={complete_calls[0]}"
        )
    finally:
        consumer.stop(timeout=2.0)
