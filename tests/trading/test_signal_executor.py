from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.signals.models import SignalEvent
from src.trading.execution_gate import ExecutionGate, ExecutionGateConfig
from src.trading.signal_executor import ExecutorConfig, TradeExecutor


def _fire(executor: TradeExecutor, event: SignalEvent) -> None:
    """Send event and wait for async worker to process it."""
    executor.on_signal_event(event)
    executor.flush()


class DummyTradingModule:
    def __init__(self):
        self.calls = []
        self.live_positions = []

    def dispatch_operation(self, operation, payload):
        self.calls.append((operation, payload))
        return {"ticket": 1, "payload": payload}

    def account_info(self):
        return {"equity": 10000.0}

    def get_positions(self, symbol=None):
        if symbol is None:
            return list(self.live_positions)
        return [row for row in self.live_positions if row.get("symbol") == symbol]


class DummyPositionManager:
    def __init__(self, positions):
        self._positions = positions

    def active_positions(self):
        return list(self._positions)



def _build_event(
    *,
    spread_points: float,
    close_price: float,
    symbol_point: float = 0.01,
    market_structure: dict | None = None,
) -> SignalEvent:
    metadata = {
        "previous_state": "armed_buy",
        "spread_points": spread_points,
        "spread_price": spread_points * symbol_point,
        "symbol_point": symbol_point,
        "close_price": close_price,
    }
    if market_structure:
        metadata["market_structure"] = market_structure
    return SignalEvent(
        symbol="XAUUSD",
        timeframe="M5",
        strategy="sma_trend",
        action="buy",
        confidence=0.9,
        signal_state="confirmed_buy",
        scope="confirmed",
        indicators={
            "atr14": {"atr": 2.0},
            "sma20": {"sma": close_price},
        },
        metadata=metadata,
        generated_at=datetime.now(timezone.utc),
        signal_id="sig_1",
        reason="test",
    )


def test_trade_executor_skips_when_spread_to_stop_ratio_is_too_high() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            sl_atr_multiplier=1.0,
            tp_atr_multiplier=2.0,
            max_spread_to_stop_ratio=0.2,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig(require_armed=True)),
    )

    _fire(executor, _build_event(spread_points=80.0, close_price=3000.0))

    assert module.calls == []
    assert executor.status()["recent_executions"][-1]["reason"] == "spread_to_stop_ratio_too_high"


def test_trade_executor_records_cost_metrics_on_success() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            sl_atr_multiplier=2.0,
            tp_atr_multiplier=4.0,
            max_spread_to_stop_ratio=0.5,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig(require_armed=True)),
    )

    _fire(executor, _build_event(spread_points=50.0, close_price=3000.0))

    assert module.calls
    cost = executor.status()["recent_executions"][-1]["cost"]
    assert cost["estimated_cost_points"] == 50.0
    assert cost["estimated_cost_price"] == 0.5
    assert cost["spread_to_stop_ratio"] is not None


def test_trade_executor_forwards_market_structure_metadata_to_dispatch() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            sl_atr_multiplier=2.0,
            tp_atr_multiplier=4.0,
            max_spread_to_stop_ratio=0.5,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig(require_armed=True)),
    )

    _fire(executor, _build_event(
            spread_points=30.0,
            close_price=3000.0,
            market_structure={
                "current_session": "new_york",
                "sweep_confirmation_state": "bullish_sweep_confirmed_previous_day_low",
                "structure_bias": "bullish_sweep_confirmed",
            },
        )
    )

    assert module.calls
    payload = module.calls[0][1]
    assert payload["metadata"]["signal"]["strategy"] == "sma_trend"
    assert payload["metadata"]["market_structure"]["sweep_confirmation_state"] == (
        "bullish_sweep_confirmed_previous_day_low"
    )


def test_trade_executor_skips_when_symbol_position_limit_is_reached() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        position_manager=DummyPositionManager(
            [{"symbol": "XAUUSD"}, {"symbol": "XAUUSD"}, {"symbol": "EURUSD"}]
        ),
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            max_concurrent_positions_per_symbol=2,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig(require_armed=True)),
    )

    _fire(executor, _build_event(spread_points=20.0, close_price=3000.0))

    assert module.calls == []
    assert executor.status()["recent_executions"][-1]["reason"] == (
        "max_concurrent_positions_per_symbol"
    )


def test_trade_executor_uses_timeframe_specific_sizing_profile() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            sl_atr_multiplier=1.5,
            tp_atr_multiplier=3.0,
            max_spread_to_stop_ratio=0.5,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig(require_armed=True)),
    )
    event = _build_event(spread_points=20.0, close_price=3000.0)
    event = SignalEvent(**{**event.__dict__, "timeframe": "M1"})

    _fire(executor, event)

    assert module.calls
    payload = module.calls[0][1]
    assert payload["sl"] == pytest.approx(2998.0)
    assert payload["tp"] == pytest.approx(3004.0)


def test_trade_executor_passes_signal_id_as_request_id() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig(require_armed=True)),
    )

    _fire(executor, _build_event(spread_points=10.0, close_price=3000.0))

    assert module.calls
    assert module.calls[0][1]["request_id"] == "sig_1"


def test_trade_executor_voting_group_strategy_blocked() -> None:
    """属于 voting group 的策略不能单独触发交易。"""
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
        ),
        execution_gate=ExecutionGate(
            ExecutionGateConfig(
                require_armed=True,
                voting_group_strategies=frozenset({"sma_trend", "supertrend"}),
            )
        ),
    )

    _fire(executor, _build_event(spread_points=20.0, close_price=3000.0))

    assert module.calls == []


def test_trade_executor_voting_group_standalone_override_allows() -> None:
    """standalone_override 中的策略即使在 voting group 中也可以单独触发。"""
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            sl_atr_multiplier=2.0,
            tp_atr_multiplier=4.0,
        ),
        execution_gate=ExecutionGate(
            ExecutionGateConfig(
                require_armed=True,
                voting_group_strategies=frozenset({"sma_trend", "supertrend"}),
                standalone_override=frozenset({"sma_trend"}),
            )
        ),
    )

    _fire(executor, _build_event(spread_points=20.0, close_price=3000.0))

    assert module.calls  # sma_trend 在 override 中，允许执行


def test_trade_executor_circuit_breaker_auto_resets() -> None:
    """熔断器在超过 circuit_auto_reset_minutes 后自动恢复。"""
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            sl_atr_multiplier=2.0,
            tp_atr_multiplier=4.0,
            max_consecutive_failures=1,
            circuit_auto_reset_minutes=10,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig(require_armed=True)),
    )
    # 手动设置熔断状态
    executor._circuit_open = True
    executor._consecutive_failures = 3
    # 设置为 15 分钟前开路
    from datetime import timedelta
    executor._circuit_open_at = datetime.now(timezone.utc) - timedelta(minutes=15)

    _fire(executor, _build_event(spread_points=20.0, close_price=3000.0))

    # 自动恢复后应该执行
    assert module.calls
    assert executor._circuit_open is False


def test_trade_executor_uses_live_positions_when_tracking_state_is_stale() -> None:
    module = DummyTradingModule()
    module.live_positions = [{"symbol": "XAUUSD"}, {"symbol": "XAUUSD"}]
    executor = TradeExecutor(
        trading_module=module,
        position_manager=DummyPositionManager([]),
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            max_concurrent_positions_per_symbol=2,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig(require_armed=True)),
    )

    _fire(executor, _build_event(spread_points=10.0, close_price=3000.0))

    assert module.calls == []
    assert executor.status()["recent_executions"][-1]["reason"] == (
        "max_concurrent_positions_per_symbol"
    )


# ── ExecutionGate 单元测试 ─────────────────────────────────────────────────

def test_execution_gate_blocks_unarmed_signal() -> None:
    gate = ExecutionGate(ExecutionGateConfig(require_armed=True))
    event = _build_event(spread_points=10.0, close_price=3000.0)
    # 覆盖 metadata，移除 armed 状态
    event = SignalEvent(**{**event.__dict__, "metadata": {"previous_state": "idle"}})

    allowed, reason = gate.check(event)

    assert not allowed
    assert reason == "require_armed"


def test_execution_gate_allows_armed_signal() -> None:
    gate = ExecutionGate(ExecutionGateConfig(require_armed=True))
    event = _build_event(spread_points=10.0, close_price=3000.0)

    allowed, reason = gate.check(event)

    assert allowed
    assert reason == ""


def test_execution_gate_blocks_strategy_not_in_whitelist() -> None:
    gate = ExecutionGate(
        ExecutionGateConfig(
            require_armed=False,
            trade_trigger_strategies=("consensus",),
        )
    )
    event = _build_event(spread_points=10.0, close_price=3000.0)

    allowed, reason = gate.check(event)

    assert not allowed
    assert reason == "not_in_trade_trigger_whitelist"


def test_execution_gate_allows_whitelisted_strategy() -> None:
    gate = ExecutionGate(
        ExecutionGateConfig(
            require_armed=False,
            trade_trigger_strategies=("sma_trend", "consensus"),
        )
    )
    event = _build_event(spread_points=10.0, close_price=3000.0)

    allowed, reason = gate.check(event)

    assert allowed
    assert reason == ""
