from datetime import datetime, timezone
from types import SimpleNamespace

from src.backtesting.tick_replay import RecoveryCanaryGatePolicy
from src.clients.mt5_market import Tick
from src.risk.service import PreTradeRiskBlockedError
from src.trading.recovery import (
    RecoveryCycleState,
    RecoveryExecutionCanaryPolicy,
    RecoveryPolicy,
)
from src.trading.recovery import canary as recovery_canary_service
from src.trading.recovery.canary import (
    close_dry_run_recovery_cycle,
    close_initial_recovery_cycle,
    evaluate_current_recovery_step,
    execute_recovery_canary,
    open_initial_recovery_cycle,
)


class DummyStateStore:
    def __init__(self):
        self.records = []

    def record_recovery_cycle_state(self, cycle, **kwargs):
        self.records.append((cycle, kwargs))


class DummyTradingPort:
    def __init__(self):
        self.dispatch_calls = []

    def dispatch_operation(self, operation, payload=None):
        self.dispatch_calls.append((operation, dict(payload or {})))
        return {
            "status": "ok",
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
            assessment={"verdict": "block", "reason": "max_trades_per_hour"},
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
                "executable": False,
            },
            "execution_state": "skipped",
        }


class CloseRecordingTradingPort:
    def __init__(self):
        self.dispatch_calls = []

    def dispatch_operation(self, operation, payload=None):
        self.dispatch_calls.append((operation, dict(payload or {})))
        if operation == "close":
            return {
                "accepted": True,
                "status": "completed",
                "result": {
                    "ticket": (payload or {}).get("ticket"),
                    "success": True,
                },
            }
        return {"status": "ok"}


class DummyPositionReader:
    def __init__(self, positions=None, error: Exception | None = None):
        self.positions = list(positions or [])
        self.error = error
        self.calls = []

    def get_positions(self, symbol=None):
        self.calls.append({"symbol": symbol})
        if self.error is not None:
            raise self.error
        return list(self.positions)


def _tick(time_msc: int, bid: float, ask: float) -> Tick:
    return Tick(
        symbol="XAUUSD",
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2,
        volume=1.0,
        time=datetime.fromtimestamp(time_msc / 1000, tz=timezone.utc),
        time_msc=time_msc,
        flags=6,
    )


def _policy(**overrides) -> RecoveryPolicy:
    values = {
        "enabled": True,
        "base_volume": 0.01,
        "multiplier": 2.0,
        "max_steps": 2,
        "max_total_volume": 0.05,
        "max_next_volume": 0.03,
        "step_distance_points": 80.0,
        "recovery_target_points": 10.0,
        "point": 0.01,
        "min_step_interval_ms": 0,
    }
    values.update(overrides)
    return RecoveryPolicy(**values)


def _cycle_state(**overrides) -> RecoveryCycleState:
    opened_at = datetime.fromtimestamp(1_000, tz=timezone.utc)
    values = {
        "cycle_id": "cycle-cleanup-alert",
        "account_key": "demo:broker:1001",
        "symbol": "XAUUSD",
        "direction": "buy",
        "status": "open",
        "base_volume": 0.01,
        "total_volume": 0.01,
        "step_count": 0,
        "average_entry_price": 100.0,
        "last_entry_price": 100.0,
        "started_at": opened_at,
        "updated_at": opened_at,
        "last_step_at": opened_at,
        "strategy": "tick_recovery_probe",
        "timeframe": "TICK",
        "source_signal_id": "sig-cleanup-alert",
    }
    values.update(overrides)
    return RecoveryCycleState(**values)


def test_execute_recovery_canary_blocks_before_state_write_when_gate_denies():
    store = DummyStateStore()
    trading = DummyTradingPort()

    result = execute_recovery_canary(
        symbol="XAUUSD",
        direction="buy",
        ticks=[_tick(1_000, 99.98, 100.00)],
        recovery_policy=_policy(),
        canary_policy=RecoveryExecutionCanaryPolicy(enabled=False, dry_run=True),
        gate_policy=RecoveryCanaryGatePolicy(min_tick_count=1),
        account_alias="demo-main",
        account_key="demo:broker:1001",
        cycle_id="cycle-blocked",
        source_signal_id="sig-blocked",
        state_store=store,
        trading_port=trading,
    )

    assert result["status"] == "blocked"
    assert "recovery_canary_not_enabled" in result["gate"]["reasons"]
    assert store.records == []
    assert trading.dispatch_calls == []


def test_execute_recovery_canary_records_cycle_and_dispatches_first_recovery_step():
    store = DummyStateStore()
    trading = DummyTradingPort()

    result = execute_recovery_canary(
        symbol="XAUUSD",
        direction="buy",
        ticks=[
            _tick(1_000, 99.98, 100.00),
            _tick(2_000, 98.98, 99.00),
            _tick(3_000, 99.55, 99.57),
        ],
        recovery_policy=_policy(),
        canary_policy=RecoveryExecutionCanaryPolicy(
            enabled=True,
            dry_run=True,
            deviation=12,
            magic=930001,
            protective_stop_points=80,
        ),
        gate_policy=RecoveryCanaryGatePolicy(min_tick_count=3, min_tick_coverage=1.0),
        account_alias="demo-main",
        account_key="demo:broker:1001",
        cycle_id="cycle-dry-run",
        source_signal_id="sig-dry-run",
        state_store=store,
        trading_port=trading,
    )

    assert result["status"] == "dry_run"
    assert result["execution"]["reason"] == "dispatched"
    assert len(store.records) == 1
    cycle, kwargs = store.records[0]
    assert cycle.cycle_id == "cycle-dry-run"
    assert cycle.account_key == "demo:broker:1001"
    assert cycle.step_count == 0
    assert cycle.total_volume == 0.01
    assert cycle.source_signal_id == "sig-dry-run"
    assert kwargs["status_reason"] == "canary_initial_state"

    operation, payload = trading.dispatch_calls[0]
    assert operation == "trade"
    assert payload["request_id"] == "recovery:cycle-dry-run:step:1"
    assert payload["trace_id"] == "sig-dry-run"
    assert payload["dry_run"] is True
    assert payload["volume"] == 0.02
    assert payload["sl"] == 98.2
    assert payload["metadata"]["execution_scope"] == "recovery_scaling"
    assert payload["metadata"]["replay"]["closed"] is True


def test_open_initial_recovery_cycle_records_real_trade_result_as_cycle_state():
    store = DummyStateStore()
    trading = DummyTradingPort()

    result = open_initial_recovery_cycle(
        symbol="XAUUSD",
        direction="buy",
        entry_price=100.0,
        recovery_policy=_policy(),
        canary_policy=RecoveryExecutionCanaryPolicy(
            enabled=True,
            dry_run=False,
            deviation=12,
            magic=930001,
            protective_stop_points=80,
        ),
        account_alias="demo-main",
        account_key="demo:broker:1001",
        cycle_id="cycle-live",
        source_signal_id="sig-live",
        state_store=store,
        trading_port=trading,
        strategy="tick_recovery_probe",
        timeframe="TICK",
    )

    assert result["status"] == "submitted"
    operation, payload = trading.dispatch_calls[0]
    assert operation == "trade"
    assert payload["request_id"] == "recovery:cycle-live:initial"
    assert payload["dry_run"] is False
    assert payload["volume"] == 0.01
    assert payload["sl"] == 99.2
    assert "created_at" not in payload
    assert payload["metadata"]["execution_scope"] == "recovery_initial"

    cycle, kwargs = store.records[0]
    assert cycle.cycle_id == "cycle-live"
    assert cycle.step_count == 0
    assert cycle.total_volume == 0.01
    assert cycle.average_entry_price == 100.0
    assert cycle.source_signal_id == "sig-live"
    assert kwargs["status_reason"] == "canary_initial_submitted"


def test_open_initial_recovery_cycle_returns_structured_pretrade_block():
    store = DummyStateStore()
    trading = RiskBlockedTradingPort()

    result = open_initial_recovery_cycle(
        symbol="XAUUSD",
        direction="buy",
        entry_price=100.0,
        recovery_policy=_policy(),
        canary_policy=RecoveryExecutionCanaryPolicy(
            enabled=True,
            dry_run=False,
            protective_stop_points=80,
        ),
        account_alias="demo-main",
        account_key="demo:broker:1001",
        cycle_id="cycle-blocked-initial",
        source_signal_id="sig-blocked-initial",
        state_store=store,
        trading_port=trading,
    )

    assert result["status"] == "blocked"
    assert result["category"] == "pre_trade_risk"
    assert result["reason"] == "max_trades_per_hour"
    assert result["payload"]["request_id"] == "recovery:cycle-blocked-initial:initial"
    assert result["pre_trade_risk"]["verdict"] == "block"
    assert store.records == []


def test_open_initial_recovery_cycle_does_not_record_dry_run_blocked_precheck():
    store = DummyStateStore()
    trading = PrecheckBlockedTradingPort()

    result = open_initial_recovery_cycle(
        symbol="XAUUSD",
        direction="buy",
        entry_price=100.0,
        recovery_policy=_policy(),
        canary_policy=RecoveryExecutionCanaryPolicy(
            enabled=True,
            dry_run=True,
            protective_stop_points=80,
        ),
        account_alias="demo-main",
        account_key="demo:broker:1001",
        cycle_id="cycle-dry-run-blocked",
        source_signal_id="sig-dry-run-blocked",
        state_store=store,
        trading_port=trading,
    )

    assert result["status"] == "blocked"
    assert result["category"] == "pre_trade_risk"
    assert result["reason"] == "Hourly trade limit reached"
    assert result["pre_trade_risk"]["executable"] is False
    assert store.records == []


def test_evaluate_current_recovery_step_uses_current_tick_and_dry_run_scaling():
    store = DummyStateStore()
    trading = DummyTradingPort()
    initial = open_initial_recovery_cycle(
        symbol="XAUUSD",
        direction="buy",
        entry_price=100.0,
        recovery_policy=_policy(step_distance_points=10.0),
        canary_policy=RecoveryExecutionCanaryPolicy(
            enabled=True,
            dry_run=True,
            protective_stop_points=80,
        ),
        account_alias="demo-main",
        account_key="demo:broker:1001",
        cycle_id="cycle-current",
        source_signal_id="sig-current",
        state_store=store,
        trading_port=trading,
    )
    cycle = initial["cycle"]

    result = evaluate_current_recovery_step(
        cycle=cycle,
        tick=_tick(2_000, 99.80, 99.82),
        recovery_policy=_policy(step_distance_points=10.0),
        canary_policy=RecoveryExecutionCanaryPolicy(
            enabled=True,
            dry_run=True,
            deviation=12,
            magic=930001,
            protective_stop_points=80,
        ),
        trading_port=trading,
    )

    assert result["status"] == "dry_run"
    assert result["decision"]["action"] == "open_step"
    operation, payload = trading.dispatch_calls[-1]
    assert operation == "trade"
    assert payload["request_id"] == "recovery:cycle-current:step:1"
    assert payload["dry_run"] is True
    assert payload["volume"] == 0.02
    assert payload["price"] == 99.82
    assert payload["sl"] == 99.02
    assert payload["metadata"]["execution_scope"] == "recovery_scaling"


def test_evaluate_current_recovery_step_records_submitted_scaling_state():
    store = DummyStateStore()
    trading = DummyTradingPort()
    initial = open_initial_recovery_cycle(
        symbol="XAUUSD",
        direction="buy",
        entry_price=100.0,
        recovery_policy=_policy(step_distance_points=10.0),
        canary_policy=RecoveryExecutionCanaryPolicy(
            enabled=True,
            dry_run=False,
            protective_stop_points=80,
        ),
        account_alias="demo-main",
        account_key="demo:broker:1001",
        cycle_id="cycle-current-submitted",
        source_signal_id="sig-current-submitted",
        state_store=store,
        trading_port=trading,
    )
    cycle = initial["cycle"]

    result = evaluate_current_recovery_step(
        cycle=cycle,
        tick=_tick(2_000, 99.80, 99.82),
        recovery_policy=_policy(step_distance_points=10.0),
        canary_policy=RecoveryExecutionCanaryPolicy(
            enabled=True,
            dry_run=False,
            deviation=12,
            magic=930001,
            protective_stop_points=80,
        ),
        trading_port=trading,
        state_store=store,
    )

    assert result["status"] == "submitted"
    assert result["updated_cycle"]["step_count"] == 1
    assert result["updated_cycle"]["total_volume"] == 0.03
    updated_cycle, kwargs = store.records[-1]
    assert updated_cycle.step_count == 1
    assert updated_cycle.total_volume == 0.03
    assert kwargs["status_reason"] == "canary_recovery_step_submitted"


def test_close_recovery_canary_positions_closes_initial_and_recovery_tickets():
    store = DummyStateStore()
    trading = CloseRecordingTradingPort()
    cycle = _cycle_state(
        cycle_id="cycle-close-recovery", source_signal_id="sig-close-recovery"
    )

    result = recovery_canary_service.close_recovery_canary_positions(
        initial_result={
            "status": "submitted",
            "result": {"ticket": 9001},
        },
        current_step_result={
            "status": "submitted",
            "updated_cycle": {
                "cycle_id": "cycle-close-recovery",
                "account_key": "demo:broker:1001",
                "symbol": "XAUUSD",
                "direction": "buy",
                "status": "open",
                "base_volume": 0.01,
                "total_volume": 0.03,
                "step_count": 1,
                "average_entry_price": 99.88,
                "last_entry_price": 99.82,
                "started_at": cycle.started_at,
                "updated_at": cycle.updated_at,
                "last_step_at": cycle.last_step_at,
                "strategy": "tick_recovery_probe",
                "timeframe": "TICK",
                "source_signal_id": "sig-close-recovery",
                "metadata": {"canary": True},
            },
            "execution": {
                "result": {"ticket": 9002},
            },
        },
        cycle_id="cycle-close-recovery",
        source_signal_id="sig-close-recovery",
        trading_port=trading,
        deviation=12,
        cycle=cycle,
        state_store=store,
    )

    assert result["status"] == "closed"
    assert result["tickets"] == [9001, 9002]
    assert [call[0] for call in trading.dispatch_calls] == ["close", "close"]
    assert (
        trading.dispatch_calls[0][1]["action_id"]
        == "recovery:cycle-close-recovery:close_initial"
    )
    assert (
        trading.dispatch_calls[1][1]["action_id"]
        == "recovery:cycle-close-recovery:close_step_1"
    )
    closed_cycle, kwargs = store.records[0]
    assert closed_cycle.status == "closed"
    assert closed_cycle.step_count == 1
    assert closed_cycle.total_volume == 0.03
    assert kwargs["status_reason"] == "canary_recovery_cycle_cleanup_closed"


def test_close_initial_recovery_cycle_dispatches_operator_close_for_submitted_ticket():
    trading = CloseRecordingTradingPort()

    result = close_initial_recovery_cycle(
        initial_result={
            "status": "submitted",
            "result": {
                "ticket": 9001,
                "volume": 0.01,
            },
        },
        cycle_id="cycle-cleanup",
        source_signal_id="sig-cleanup",
        trading_port=trading,
        deviation=12,
    )

    assert result["status"] == "closed"
    assert result["ticket"] == 9001
    operation, payload = trading.dispatch_calls[0]
    assert operation == "close"
    assert payload["ticket"] == 9001
    assert payload["deviation"] == 12
    assert payload["actor"] == "recovery_canary"
    assert payload["reason"] == "demo_recovery_canary_cleanup"
    assert payload["action_id"] == "recovery:cycle-cleanup:close_initial"
    assert payload["request_context"]["trace_id"] == "sig-cleanup"
    assert payload["request_context"]["cycle_id"] == "cycle-cleanup"
    assert payload["request_context"]["execution_scope"] == "recovery_initial_cleanup"


def test_close_initial_recovery_cycle_skips_when_initial_was_not_submitted():
    trading = CloseRecordingTradingPort()

    result = close_initial_recovery_cycle(
        initial_result={"status": "dry_run", "result": {"ticket": 9001}},
        cycle_id="cycle-dry-run-cleanup",
        source_signal_id="sig-dry-run-cleanup",
        trading_port=trading,
        deviation=12,
    )

    assert result == {
        "status": "skipped",
        "reason": "initial_not_submitted",
    }
    assert trading.dispatch_calls == []


def test_close_dry_run_recovery_cycle_marks_cycle_closed_without_trade_call():
    store = DummyStateStore()
    cycle = _cycle_state(cycle_id="cycle-dry-run-finalize", status="open")

    result = close_dry_run_recovery_cycle(
        cycle=cycle,
        state_store=store,
    )

    assert result["status"] == "closed"
    closed_cycle, kwargs = store.records[0]
    assert closed_cycle.status == "closed"
    assert closed_cycle.cycle_id == "cycle-dry-run-finalize"
    assert kwargs["status_reason"] == "canary_dry_run_cycle_closed"
    assert kwargs["closed_at"] is not None
    assert kwargs["close_price"] is None
    assert kwargs["realized_pnl"] is None


def test_close_initial_recovery_cycle_marks_cycle_closed_when_state_store_provided():
    store = DummyStateStore()
    trading = CloseRecordingTradingPort()
    opened_at = datetime.fromtimestamp(1_000, tz=timezone.utc)
    cycle = RecoveryCycleState(
        cycle_id="cycle-close-state",
        account_key="demo:broker:1001",
        symbol="XAUUSD",
        direction="buy",
        status="open",
        base_volume=0.01,
        total_volume=0.01,
        step_count=0,
        average_entry_price=100.0,
        last_entry_price=100.0,
        started_at=opened_at,
        updated_at=opened_at,
        last_step_at=opened_at,
        strategy="tick_recovery_probe",
        timeframe="TICK",
        source_signal_id="sig-close-state",
    )

    result = close_initial_recovery_cycle(
        initial_result={
            "status": "submitted",
            "result": {"ticket": 9001},
        },
        cycle_id="cycle-close-state",
        source_signal_id="sig-close-state",
        trading_port=trading,
        deviation=12,
        cycle=cycle,
        state_store=store,
    )

    assert result["status"] == "closed"
    closed_cycle, kwargs = store.records[0]
    assert closed_cycle.status == "closed"
    assert closed_cycle.cycle_id == "cycle-close-state"
    assert kwargs["status_reason"] == "canary_initial_cleanup_closed"
    assert kwargs["closed_at"] is not None


def test_verify_initial_recovery_cleanup_records_critical_alert_when_ticket_still_open():
    store = DummyStateStore()
    cycle = _cycle_state()
    reader = DummyPositionReader(
        [
            SimpleNamespace(
                ticket=9001,
                symbol="XAUUSD",
                volume=0.01,
                price_open=100.0,
            )
        ]
    )

    result = recovery_canary_service.verify_initial_recovery_cleanup(
        cleanup_result={
            "status": "failed",
            "reason": "cleanup_dispatched",
            "ticket": 9001,
            "result": {"success": False},
        },
        cycle=cycle,
        cycle_id="cycle-cleanup-alert",
        source_signal_id="sig-cleanup-alert",
        state_store=store,
        position_reader=reader,
    )

    assert result["status"] == "critical"
    assert result["position_found"] is True
    assert result["alert"]["severity"] == "critical"
    assert result["alert"]["code"] == "recovery_initial_cleanup_unresolved"
    assert result["alert"]["ticket"] == 9001
    assert reader.calls == [{"symbol": "XAUUSD"}]

    blocked_cycle, kwargs = store.records[0]
    assert blocked_cycle.status == "blocked"
    assert blocked_cycle.cycle_id == "cycle-cleanup-alert"
    assert (
        blocked_cycle.metadata["cleanup_alert"]["code"]
        == "recovery_initial_cleanup_unresolved"
    )
    assert kwargs["status_reason"] == "canary_initial_cleanup_unresolved"


def test_verify_initial_recovery_cleanup_resolves_failed_close_when_ticket_is_absent():
    store = DummyStateStore()
    cycle = _cycle_state()
    reader = DummyPositionReader([])

    result = recovery_canary_service.verify_initial_recovery_cleanup(
        cleanup_result={"status": "failed", "ticket": 9001},
        cycle=cycle,
        cycle_id="cycle-cleanup-absent",
        source_signal_id="sig-cleanup-absent",
        state_store=store,
        position_reader=reader,
    )

    assert result["status"] == "resolved_after_verify"
    assert result["position_found"] is False
    assert "alert" not in result
    assert store.records == []


def test_verify_initial_recovery_cleanup_records_critical_alert_when_verification_fails():
    store = DummyStateStore()
    cycle = _cycle_state()
    reader = DummyPositionReader(error=RuntimeError("mt5 position read failed"))

    result = recovery_canary_service.verify_initial_recovery_cleanup(
        cleanup_result={"status": "failed", "ticket": 9001},
        cycle=cycle,
        cycle_id="cycle-cleanup-unknown",
        source_signal_id="sig-cleanup-unknown",
        state_store=store,
        position_reader=reader,
    )

    assert result["status"] == "critical"
    assert result["position_found"] is None
    assert result["alert"]["severity"] == "critical"
    assert result["alert"]["code"] == "recovery_initial_cleanup_verification_failed"
    assert "mt5 position read failed" in result["alert"]["message"]
    blocked_cycle, kwargs = store.records[0]
    assert blocked_cycle.status == "blocked"
    assert (
        blocked_cycle.metadata["cleanup_alert"]["code"]
        == "recovery_initial_cleanup_verification_failed"
    )
    assert kwargs["status_reason"] == "canary_initial_cleanup_verification_failed"
