from __future__ import annotations

from src.config.mt5 import MT5Settings
from src.config.runtime_identity import RuntimeIdentity, build_account_key
from src.monitoring.pipeline import (
    PIPELINE_COMMAND_COMPLETED,
    PIPELINE_COMMAND_FAILED,
    PIPELINE_COMMAND_SUBMITTED,
    PipelineEventBus,
)
from src.trading.commands.results import build_operator_command_result
from src.trading.commands.consumer import OperatorCommandConsumer
from src.trading.commands.service import OperatorCommandService


def _runtime_identity(
    *,
    instance_role: str = "main",
    live_topology_mode: str = "single_account",
    instance_name: str = "live-main",
    environment: str = "live",
    account_alias: str = "main",
    login: int = 1001,
) -> RuntimeIdentity:
    return RuntimeIdentity(
        instance_name=instance_name,
        environment=environment,
        instance_id=f"{instance_role}-{instance_name}",
        instance_role=instance_role,
        live_topology_mode=live_topology_mode,
        account_alias=account_alias,
        account_label=account_alias.title(),
        account_key=build_account_key(environment, "Broker-Live", login),
        mt5_server="Broker-Live",
        mt5_login=login,
        mt5_path=f"C:/MT5/{account_alias}/terminal64.exe",
        peer_main_instance_id=f"{environment}:{instance_name}",
    )


def test_operator_command_service_enqueue_emits_command_submitted(monkeypatch):
    rows: list[tuple] = []
    received = []
    pipeline_bus = PipelineEventBus()
    pipeline_bus.add_listener(received.append)
    monkeypatch.setattr(
        "src.trading.commands.service.load_group_mt5_settings",
        lambda **kwargs: {
            "main": MT5Settings(
                instance_name="live-main",
                account_alias="main",
                account_label="Main",
                mt5_login=1001,
                mt5_server="Broker-Live",
                mt5_path="C:/MT5/main/terminal64.exe",
            ),
            "exec_a": MT5Settings(
                instance_name="live-exec-a",
                account_alias="exec_a",
                account_label="Exec A",
                mt5_login=1002,
                mt5_server="Broker-Live",
                mt5_path="C:/MT5/exec_a/terminal64.exe",
            ),
        },
    )
    service = OperatorCommandService(
        write_fn=lambda batch: rows.extend(batch),
        fetch_fn=lambda **kwargs: [],
        runtime_identity=_runtime_identity(),
        pipeline_event_bus=pipeline_bus,
    )

    response = service.enqueue(
        command_type="set_trade_control",
        payload={"trace_id": "trace-command-1", "symbol": "XAUUSD"},
        actor="tester",
        reason="manual",
        action_id="action-command-1",
        idempotency_key="idem-command-1",
        request_context={"source": "test"},
        target_account_alias="exec_a",
    )

    assert response["accepted"] is True
    assert response["status"] == "pending"
    assert len(rows) == 1
    assert rows[0][3] == build_account_key("live", "Broker-Live", 1002)
    assert rows[0][4] == "exec_a"
    assert [event.type for event in received] == [PIPELINE_COMMAND_SUBMITTED]
    assert received[0].trace_id == "trace-command-1"
    assert received[0].payload["submitted_by_instance_id"] == "main-live-main"
    assert received[0].payload["submitted_by_account_key"] == build_account_key(
        "live",
        "Broker-Live",
        1001,
    )
    assert received[0].payload["target_account_alias"] == "exec_a"


def test_operator_command_consumer_process_command_emits_completed_trace():
    received = []
    completed = []
    projected = []
    pipeline_bus = PipelineEventBus()
    pipeline_bus.add_listener(received.append)

    class _CommandService:
        def update_trade_control(self, **kwargs):
            return {"auto_entry_enabled": False, "close_only_mode": True}

        def record_operator_action(self, **kwargs):
            return {"operation_id": "audit-command-1"}

    class _Projector:
        def project_now(self):
            projected.append("projected")

    consumer = OperatorCommandConsumer(
        claim_fn=lambda **kwargs: [],
        complete_fn=lambda **kwargs: completed.append(kwargs),
        runtime_identity=_runtime_identity(
            instance_role="executor",
            live_topology_mode="multi_account",
            instance_name="live-exec-a",
            account_alias="exec_a",
            login=1002,
        ),
        command_service=_CommandService(),
        account_risk_state_projector=_Projector(),
        pipeline_event_bus=pipeline_bus,
    )

    consumer._process_command(
        {
            "command_id": "command-1",
            "command_type": "set_trade_control",
            "target_account_key": build_account_key("live", "Broker-Live", 1002),
            "target_account_alias": "exec_a",
            "action_id": "action-command-1",
            "actor": "tester",
            "reason": "manual",
            "idempotency_key": "idem-command-1",
            "request_context": {"source": "test"},
            "payload": {
                "trace_id": "trace-command-2",
                "auto_entry_enabled": False,
                "close_only_mode": True,
            },
        }
    )

    assert completed == [
        {
            "command_id": "command-1",
            "status": "completed",
            "claimed_by_instance_id": "executor-live-exec-a",
            "claimed_by_run_id": "executor-live-exec-a",
            "response_payload": {
                "accepted": True,
                "status": "applied",
                "action_id": "action-command-1",
                "command_id": "command-1",
                "audit_id": "audit-command-1",
                "actor": "tester",
                "reason": "manual",
                "idempotency_key": "idem-command-1",
                "request_context": {"source": "test"},
                "message": "trade control updated",
                "error_code": None,
                "recorded_at": completed[0]["response_payload"]["recorded_at"],
                "effective_state": {
                    "trade_control": {
                        "auto_entry_enabled": False,
                        "close_only_mode": True,
                    }
                },
            },
            "audit_id": "audit-command-1",
            "last_error_code": None,
        }
    ]
    assert projected == ["projected"]
    assert [event.type for event in received] == [PIPELINE_COMMAND_COMPLETED]
    assert received[0].trace_id == "trace-command-2"
    assert received[0].payload["command_id"] == "command-1"
    assert received[0].payload["claimed_by_instance_id"] == "executor-live-exec-a"
    assert received[0].payload["account_key"] == build_account_key("live", "Broker-Live", 1002)
    assert received[0].payload["audit_id"] == "audit-command-1"


def test_operator_command_consumer_worker_emits_dead_lettered_failure(monkeypatch):
    received = []
    pipeline_bus = PipelineEventBus()
    pipeline_bus.add_listener(received.append)

    consumer = OperatorCommandConsumer(
        claim_fn=lambda **kwargs: [],
        complete_fn=lambda **kwargs: None,
        runtime_identity=_runtime_identity(
            instance_role="executor",
            live_topology_mode="multi_account",
            instance_name="live-exec-a",
            account_alias="exec_a",
            login=1002,
        ),
        command_service=object(),
        pipeline_event_bus=pipeline_bus,
    )

    calls = {"count": 0}

    def _claim_fn(**kwargs):
        calls["count"] += 1
        consumer._stop_event.set()
        return {
            "claimed": [],
            "reclaimed": [],
            "dead_lettered": [
                {
                    "command_id": "command-dead",
                    "command_type": "close_position",
                    "target_account_key": build_account_key("live", "Broker-Live", 1002),
                    "target_account_alias": "exec_a",
                    "payload": {"trace_id": "trace-command-dead", "ticket": 42},
                    "last_error_code": "command_attempts_exhausted",
                }
            ],
        }

    consumer._claim_fn = _claim_fn
    monkeypatch.setattr("src.trading.commands.consumer.time.sleep", lambda *_args, **_kwargs: None)

    consumer._worker()

    assert calls["count"] == 1
    assert [event.type for event in received] == [PIPELINE_COMMAND_FAILED]
    assert received[0].trace_id == "trace-command-dead"
    assert received[0].payload["status"] == "dead_lettered"
    assert received[0].payload["error_code"] == "command_attempts_exhausted"


def test_operator_command_consumer_close_position_forwards_volume():
    captured: dict[str, object] = {}

    class _CommandService:
        def close_position(self, **kwargs):
            captured.update(kwargs)
            return build_operator_command_result(
                accepted=True,
                status="completed",
                action_id=kwargs["action_id"],
                audit_id=kwargs["audit_id"],
                actor=kwargs["actor"],
                reason=kwargs["reason"],
                idempotency_key=kwargs["idempotency_key"],
                request_context=kwargs["request_context"],
                message="position close completed",
                effective_state={"result": {"success": True}},
            )

    consumer = OperatorCommandConsumer(
        claim_fn=lambda **kwargs: [],
        complete_fn=lambda **kwargs: None,
        runtime_identity=_runtime_identity(),
        command_service=_CommandService(),
    )

    response = consumer._execute_command(
        {
            "command_id": "command-close-volume",
            "command_type": "close_position",
            "action_id": "action-close-volume",
            "actor": "tester",
            "reason": "reduce_only",
            "payload": {
                "ticket": 101,
                "volume": 0.12,
                "deviation": 15,
                "comment": "partial_close",
            },
        }
    )

    assert captured["ticket"] == 101
    assert captured["volume"] == 0.12
    assert response["accepted"] is True


def test_operator_command_consumer_cancel_orders_forwards_magic():
    captured: dict[str, object] = {}

    class _CommandService:
        def cancel_orders(self, **kwargs):
            captured.update(kwargs)
            return build_operator_command_result(
                accepted=True,
                status="completed",
                action_id=kwargs["action_id"],
                audit_id=kwargs["audit_id"],
                actor=kwargs["actor"],
                reason=kwargs["reason"],
                idempotency_key=kwargs["idempotency_key"],
                request_context=kwargs["request_context"],
                message="cancel orders completed",
                effective_state={"result": {"canceled": []}},
            )

    consumer = OperatorCommandConsumer(
        claim_fn=lambda **kwargs: [],
        complete_fn=lambda **kwargs: None,
        runtime_identity=_runtime_identity(),
        command_service=_CommandService(),
    )

    response = consumer._execute_command(
        {
            "command_id": "command-cancel-magic",
            "command_type": "cancel_orders",
            "action_id": "action-cancel-magic",
            "actor": "tester",
            "reason": "manual_cancel",
            "payload": {
                "symbol": "XAUUSD",
                "magic": 987654,
            },
        }
    )

    assert captured["symbol"] == "XAUUSD"
    assert captured["magic"] == 987654
    assert response["accepted"] is True
