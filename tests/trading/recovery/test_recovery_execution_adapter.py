from datetime import datetime, timezone

from src.risk.service import PreTradeRiskBlockedError
from src.trading.application.module import TradingModule
from src.trading.recovery import (
    PositionScalingIntent,
    RecoveryCycleState,
    RecoveryExecutionAdapter,
    RecoveryExecutionCanaryPolicy,
    RecoveryPolicy,
)
from tests.trading.test_trading_module import DummyDBWriter, DummyRegistry


class DummyTradingPort:
    def __init__(self):
        self.dispatch_calls = []

    def dispatch_operation(self, operation, payload=None):
        self.dispatch_calls.append((operation, dict(payload or {})))
        return {
            "status": "ok",
            "ticket": 9001,
            "request_id": (payload or {}).get("request_id"),
            "dry_run": bool((payload or {}).get("dry_run")),
        }


class RiskBlockedTradingPort:
    def __init__(self):
        self.dispatch_calls = []

    def dispatch_operation(self, operation, payload=None):
        self.dispatch_calls.append((operation, dict(payload or {})))
        raise PreTradeRiskBlockedError(
            "Hourly trade limit reached",
            assessment={
                "verdict": "block",
                "reason": "max_trades_per_hour",
                "checks": [
                    {
                        "name": "trade_frequency",
                        "verdict": "block",
                        "message": "Hourly trade limit reached",
                    }
                ],
            },
        )


class PrecheckBlockedTradingPort:
    def __init__(self):
        self.dispatch_calls = []

    def dispatch_operation(self, operation, payload=None):
        self.dispatch_calls.append((operation, dict(payload or {})))
        return {
            "request_id": (payload or {}).get("request_id"),
            "dry_run": True,
            "precheck": {
                "blocked": True,
                "verdict": "block",
                "reason": "Hourly trade limit reached",
                "checks": [
                    {
                        "name": "max_trades_per_hour",
                        "verdict": "block",
                        "reason": "Hourly trade limit reached",
                    }
                ],
                "executable": False,
            },
            "execution_state": "skipped",
        }


def _policy(**overrides) -> RecoveryPolicy:
    values = {
        "enabled": True,
        "base_volume": 0.01,
        "multiplier": 2.0,
        "max_steps": 3,
        "max_total_volume": 0.08,
        "max_next_volume": 0.04,
        "step_distance_points": 100.0,
        "recovery_target_points": 20.0,
        "point": 0.01,
    }
    values.update(overrides)
    return RecoveryPolicy(**values)


def _cycle(**overrides) -> RecoveryCycleState:
    now = datetime(2026, 5, 6, 1, 2, 3, tzinfo=timezone.utc)
    values = {
        "cycle_id": "cycle-1",
        "account_key": "demo:broker:1001",
        "symbol": "XAUUSD",
        "direction": "buy",
        "status": "open",
        "base_volume": 0.01,
        "total_volume": 0.03,
        "step_count": 1,
        "average_entry_price": 2298.5,
        "last_entry_price": 2297.0,
        "started_at": now,
        "updated_at": now,
        "last_step_at": now,
        "strategy": "tick_recovery_probe",
        "timeframe": "TICK",
        "source_signal_id": "sig-1",
    }
    values.update(overrides)
    return RecoveryCycleState(**values)


def _intent(**overrides) -> PositionScalingIntent:
    values = {
        "cycle_id": "cycle-1",
        "account_key": "demo:broker:1001",
        "symbol": "XAUUSD",
        "direction": "buy",
        "strategy": "tick_recovery_probe",
        "timeframe": "TICK",
        "source_signal_id": "sig-1",
        "step_index": 2,
        "volume": 0.02,
        "entry_price": 2296.0,
        "reason": "adverse_move_triggered",
        "metadata": {"spread": 0.2},
        "created_at": datetime(2026, 5, 6, 1, 2, 4, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return PositionScalingIntent(**values)


def test_recovery_execution_adapter_blocks_when_canary_disabled():
    trading = DummyTradingPort()
    adapter = RecoveryExecutionAdapter(trading_port=trading)

    result = adapter.execute_scaling_intent(
        recovery_policy=_policy(),
        cycle=_cycle(),
        intent=_intent(),
        canary_policy=RecoveryExecutionCanaryPolicy(enabled=False),
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "recovery_canary_disabled"
    assert trading.dispatch_calls == []


def test_recovery_execution_canary_policy_builds_from_config_object():
    class Config:
        enabled = True
        dry_run = False
        order_kind = "market"
        deviation = 15
        magic = 930002
        comment_prefix = "recovery-demo"
        protective_stop_points = 80

    policy = RecoveryExecutionCanaryPolicy.from_config(Config())

    assert policy.enabled is True
    assert policy.dry_run is False
    assert policy.order_kind == "market"
    assert policy.deviation == 15
    assert policy.magic == 930002
    assert policy.comment_prefix == "recovery-demo"
    assert policy.protective_stop_points == 80


def test_recovery_execution_canary_policy_parses_dict_boolean_strings():
    policy = RecoveryExecutionCanaryPolicy.from_config(
        {
            "enabled": "false",
            "dry_run": "false",
            "order_kind": "market",
            "deviation": "12",
            "magic": "930001",
            "comment_prefix": "recovery-demo",
            "protective_stop_points": "75",
        }
    )

    assert policy.enabled is False
    assert policy.dry_run is False
    assert policy.deviation == 12
    assert policy.magic == 930001
    assert policy.protective_stop_points == 75.0


def test_recovery_execution_adapter_blocks_guard_failure_before_dispatch():
    trading = DummyTradingPort()
    adapter = RecoveryExecutionAdapter(trading_port=trading)

    result = adapter.execute_scaling_intent(
        recovery_policy=_policy(),
        cycle=_cycle(step_count=1),
        intent=_intent(step_index=3),
        canary_policy=RecoveryExecutionCanaryPolicy(enabled=True, dry_run=True),
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "step_index_mismatch"
    assert result["guard"]["allowed"] is False
    assert trading.dispatch_calls == []


def test_recovery_execution_adapter_requires_protective_stop_for_market_orders():
    trading = DummyTradingPort()
    adapter = RecoveryExecutionAdapter(trading_port=trading)

    result = adapter.execute_scaling_intent(
        recovery_policy=_policy(),
        cycle=_cycle(),
        intent=_intent(),
        canary_policy=RecoveryExecutionCanaryPolicy(enabled=True, dry_run=True),
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "recovery_protective_stop_required"
    assert result["category"] == "recovery_canary"
    assert trading.dispatch_calls == []


def test_recovery_execution_adapter_dispatches_formal_trade_payload_when_enabled():
    trading = DummyTradingPort()
    adapter = RecoveryExecutionAdapter(trading_port=trading)

    result = adapter.execute_scaling_intent(
        recovery_policy=_policy(),
        cycle=_cycle(),
        intent=_intent(),
        canary_policy=RecoveryExecutionCanaryPolicy(
            enabled=True,
            dry_run=True,
            deviation=12,
            magic=930001,
            protective_stop_points=80,
        ),
    )

    assert result["status"] == "dry_run"
    assert result["reason"] == "dispatched"
    assert len(trading.dispatch_calls) == 1
    operation, payload = trading.dispatch_calls[0]
    assert operation == "trade"
    assert payload["symbol"] == "XAUUSD"
    assert payload["side"] == "buy"
    assert payload["volume"] == 0.02
    assert payload["price"] == 2296.0
    assert payload["sl"] == 2295.2
    assert payload["dry_run"] is True
    assert payload["deviation"] == 12
    assert payload["magic"] == 930001
    assert payload["request_id"] == "recovery:cycle-1:step:2"
    assert payload["action_id"] == "recovery:cycle-1:step:2"
    assert payload["trace_id"] == "sig-1"
    assert payload["metadata"]["execution_scope"] == "recovery_scaling"
    assert payload["metadata"]["recovery_cycle_id"] == "cycle-1"
    assert payload["metadata"]["recovery_step_index"] == 2
    assert payload["metadata"]["source_signal_id"] == "sig-1"
    assert payload["metadata"]["guard"]["projected_total_volume"] == 0.05


def test_recovery_execution_adapter_returns_structured_pretrade_block():
    trading = RiskBlockedTradingPort()
    adapter = RecoveryExecutionAdapter(trading_port=trading)

    result = adapter.execute_scaling_intent(
        recovery_policy=_policy(),
        cycle=_cycle(),
        intent=_intent(),
        canary_policy=RecoveryExecutionCanaryPolicy(
            enabled=True,
            dry_run=True,
            protective_stop_points=80,
        ),
    )

    assert result["status"] == "skipped"
    assert result["category"] == "pre_trade_risk"
    assert result["reason"] == "max_trades_per_hour"
    assert result["pre_trade_risk"]["verdict"] == "block"
    assert result["payload"]["request_id"] == "recovery:cycle-1:step:2"
    assert len(trading.dispatch_calls) == 1


def test_recovery_execution_adapter_treats_dry_run_blocked_precheck_as_skipped():
    trading = PrecheckBlockedTradingPort()
    adapter = RecoveryExecutionAdapter(trading_port=trading)

    result = adapter.execute_scaling_intent(
        recovery_policy=_policy(),
        cycle=_cycle(),
        intent=_intent(),
        canary_policy=RecoveryExecutionCanaryPolicy(
            enabled=True,
            dry_run=True,
            protective_stop_points=80,
        ),
    )

    assert result["status"] == "skipped"
    assert result["category"] == "pre_trade_risk"
    assert result["reason"] == "Hourly trade limit reached"
    assert result["pre_trade_risk"]["executable"] is False
    assert result["result"]["execution_state"] == "skipped"


def test_recovery_execution_adapter_uses_trading_module_command_audit_contract():
    db = DummyDBWriter()
    module = TradingModule(registry=DummyRegistry(), db_writer=db)
    adapter = RecoveryExecutionAdapter(trading_port=module)

    result = adapter.execute_scaling_intent(
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
    assert db.rows[-1][3] == "execute_trade"
    assert db.rows[-1][4] == "success"
    assert db.rows[-1][15]["request_id"] == "recovery:cycle-1:step:2"
    assert db.rows[-1][15]["metadata"]["execution_scope"] == "recovery_scaling"
    assert db.rows[-1][15]["metadata"]["recovery_cycle_id"] == "cycle-1"
    assert db.rows[-1][15]["metadata"]["recovery_step_index"] == 2
    assert db.rows[-1][15]["metadata"]["source_signal_id"] == "sig-1"
