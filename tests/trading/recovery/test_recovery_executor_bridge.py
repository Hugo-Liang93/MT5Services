from datetime import datetime, timezone

from src.trading.execution.executor import ExecutorConfig, TradeExecutor
from src.trading.recovery import (
    PositionScalingIntent,
    RecoveryCycleState,
    RecoveryExecutionCanaryPolicy,
    RecoveryPolicy,
)


class DummyRuntimeIdentity:
    environment = "demo"
    instance_id = "exec-1"
    instance_name = "demo-main"
    instance_role = "executor"
    run_id = "run-1"
    account_key = "demo:broker:1001"
    account_alias = "demo-main"


class DummyTradingPort:
    def __init__(self):
        self.dispatch_calls = []

    def dispatch_operation(self, operation, payload=None):
        self.dispatch_calls.append((operation, dict(payload or {})))
        return {"status": "ok", "request_id": (payload or {}).get("request_id")}

    def get_positions(self, symbol=None, magic=None):
        return []

    def get_orders(self, symbol=None, magic=None):
        return []


def _policy() -> RecoveryPolicy:
    return RecoveryPolicy(
        enabled=True,
        base_volume=0.01,
        multiplier=2.0,
        max_steps=3,
        max_total_volume=0.08,
        max_next_volume=0.04,
        step_distance_points=100.0,
        recovery_target_points=20.0,
        point=0.01,
    )


def _cycle() -> RecoveryCycleState:
    now = datetime(2026, 5, 6, 1, 2, 3, tzinfo=timezone.utc)
    return RecoveryCycleState(
        cycle_id="cycle-1",
        account_key="demo:broker:1001",
        symbol="XAUUSD",
        direction="buy",
        status="open",
        base_volume=0.01,
        total_volume=0.03,
        step_count=1,
        average_entry_price=2298.5,
        last_entry_price=2297.0,
        started_at=now,
        updated_at=now,
        last_step_at=now,
        strategy="tick_recovery_probe",
        timeframe="TICK",
        source_signal_id="sig-1",
    )


def _intent() -> PositionScalingIntent:
    return PositionScalingIntent(
        cycle_id="cycle-1",
        account_key="demo:broker:1001",
        symbol="XAUUSD",
        direction="buy",
        strategy="tick_recovery_probe",
        timeframe="TICK",
        source_signal_id="sig-1",
        step_index=2,
        volume=0.02,
        entry_price=2296.0,
        reason="adverse_move_triggered",
        created_at=datetime(2026, 5, 6, 1, 2, 4, tzinfo=timezone.utc),
    )


def test_trade_executor_exposes_explicit_recovery_scaling_intent_boundary():
    trading = DummyTradingPort()
    executor = TradeExecutor(
        trading_module=trading,
        runtime_identity=DummyRuntimeIdentity(),
        config=ExecutorConfig(enabled=True),
    )

    result = executor.execute_recovery_scaling_intent(
        recovery_policy=_policy(),
        cycle=_cycle(),
        intent=_intent(),
        canary_policy=RecoveryExecutionCanaryPolicy(
            enabled=True,
            dry_run=True,
            protective_stop_points=80,
        ),
    )

    assert result["status"] == "dry_run"
    assert len(trading.dispatch_calls) == 1
    operation, payload = trading.dispatch_calls[0]
    assert operation == "trade"
    assert payload["request_id"] == "recovery:cycle-1:step:2"
    assert payload["metadata"]["execution_scope"] == "recovery_scaling"
