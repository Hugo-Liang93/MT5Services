from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.monitoring.pipeline import (
    PIPELINE_EXECUTION_BLOCKED,
    PIPELINE_EXECUTION_DECIDED,
    PIPELINE_EXECUTION_SUBMITTED,
    PipelineEventBus,
)
from src.signals.models import SignalEvent
from src.trading.execution import ExecutionGate, ExecutionGateConfig
from src.trading.execution import TradeParameters
from src.trading.execution import ExecutorConfig, TradeExecutor


def _fire(executor: TradeExecutor, event: SignalEvent) -> None:
    """Send event and wait for async worker to process it."""
    executor.on_signal_event(event)
    executor.flush()


class DummyTradingModule:
    def __init__(self):
        self.calls = []
        self.live_positions = []
        self.live_orders = []

    def dispatch_operation(self, operation, payload):
        self.calls.append((operation, payload))
        return {"ticket": 1, "payload": payload}

    def account_info(self):
        return {"equity": 10000.0}

    def get_positions(self, symbol=None):
        if symbol is None:
            return list(self.live_positions)
        return [row for row in self.live_positions if row.get("symbol") == symbol]

    def get_orders(self, symbol=None):
        if symbol is None:
            return list(self.live_orders)
        return [row for row in self.live_orders if row.get("symbol") == symbol]


class DummyPositionManager:
    def __init__(self, positions):
        self._positions = positions
        self.track_calls = []

    def active_positions(self):
        return list(self._positions)

    def track_position(self, **kwargs):
        self.track_calls.append(kwargs)
        tracked = {
            "ticket": kwargs["ticket"],
            "symbol": kwargs["symbol"],
            "timeframe": kwargs.get("timeframe"),
            "strategy": kwargs.get("strategy"),
            "action": kwargs.get("action"),
        }
        self._positions.append(tracked)
        return tracked


class DummyTradeOutcomeTracker:
    def __init__(self):
        self.opened = []

    def on_trade_opened(self, **kwargs):
        self.opened.append(kwargs)


class DummyPendingManager:
    def __init__(self, contexts=None, *, config=None):
        self._contexts = list(contexts or [])
        self.config = config
        self.tracked_orders = []

    def active_execution_contexts(self):
        return list(self._contexts)

    def status(self):
        return {"active_count": len(self._contexts), "entries": list(self._contexts), "stats": {}}

    def track_mt5_order(self, **kwargs):
        self.tracked_orders.append(kwargs)



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
        "signal_trace_id": "trace_sig_1",
    }
    if market_structure:
        metadata["market_structure"] = market_structure
    return SignalEvent(
        symbol="XAUUSD",
        timeframe="M5",
        strategy="sma_trend",
        direction="buy",
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

    # M5 SL = 2.0 ATR × 2.0 = 4.0 points → spread 120 / (4.0 × 100) = 0.30 > 0.2
    _fire(executor, _build_event(spread_points=120.0, close_price=3000.0))

    assert module.calls == []
    assert executor.status()["recent_executions"][-1]["reason"] == "spread_to_stop_ratio_too_high"


def test_trade_executor_records_cost_metrics_on_success() -> None:
    module = DummyTradingModule()
    pipeline_bus = PipelineEventBus()
    received = []
    pipeline_bus.add_listener(received.append)
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
        pipeline_event_bus=pipeline_bus,
    )

    _fire(executor, _build_event(spread_points=50.0, close_price=3000.0))

    assert module.calls
    cost = executor.status()["recent_executions"][-1]["cost"]
    assert cost["estimated_cost_points"] == 50.0
    assert cost["estimated_cost_price"] == 0.5
    assert cost["spread_to_stop_ratio"] is not None
    assert [event.type for event in received][-2:] == [
        PIPELINE_EXECUTION_DECIDED,
        PIPELINE_EXECUTION_SUBMITTED,
    ]


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
    pipeline_bus = PipelineEventBus()
    received = []
    pipeline_bus.add_listener(received.append)
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
        pipeline_event_bus=pipeline_bus,
    )

    _fire(executor, _build_event(spread_points=20.0, close_price=3000.0))

    assert module.calls == []
    assert executor.status()["recent_executions"][-1]["reason"] == (
        "max_concurrent_positions_per_symbol"
    )
    assert received[-1].type == PIPELINE_EXECUTION_BLOCKED
    assert received[-1].payload["reason"] == "position_limit"


def test_trade_executor_skips_when_same_strategy_direction_position_is_active() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        position_manager=DummyPositionManager(
            [
                {
                    "symbol": "XAUUSD",
                    "timeframe": "M5",
                    "strategy": "sma_trend",
                    "action": "buy",
                }
            ]
        ),
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            max_concurrent_positions_per_symbol=3,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig(require_armed=True)),
    )

    _fire(executor, _build_event(spread_points=20.0, close_price=3000.0))

    assert module.calls == []
    assert executor.status()["recent_executions"][-1]["reason"] == (
        "position_same_strategy_direction"
    )


def test_trade_executor_allows_reentry_after_same_strategy_direction_position_is_closed() -> None:
    module = DummyTradingModule()
    tracked_positions = [
        {
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "strategy": "sma_trend",
            "action": "buy",
        }
    ]
    executor = TradeExecutor(
        trading_module=module,
        position_manager=DummyPositionManager(tracked_positions),
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            max_concurrent_positions_per_symbol=3,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig(require_armed=True)),
    )

    _fire(executor, _build_event(spread_points=20.0, close_price=3000.0))
    tracked_positions.clear()
    event = SignalEvent(
        **{
            **_build_event(spread_points=20.0, close_price=3000.0).__dict__,
            "signal_id": "sig_2",
            "generated_at": datetime.now(timezone.utc),
        }
    )

    _fire(executor, event)

    assert len(module.calls) == 1
    assert module.calls[0][1]["request_id"] == "sig_2"


def test_trade_executor_skips_when_same_strategy_direction_mt5_pending_order_exists() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        pending_entry_manager=DummyPendingManager(
            [
                {
                    "symbol": "XAUUSD",
                    "timeframe": "M5",
                    "strategy": "sma_trend",
                    "direction": "buy",
                    "source": "mt5_order",
                }
            ]
        ),
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig(require_armed=True)),
    )

    _fire(executor, _build_event(spread_points=20.0, close_price=3000.0))

    assert module.calls == []
    assert executor.status()["recent_executions"][-1]["reason"] == (
        "pending_entry_same_strategy_direction"
    )


def test_trade_executor_pending_submission_sets_reentry_cooldown_anchor() -> None:
    from src.trading.pending import PendingEntryConfig

    module = DummyTradingModule()
    pending_manager = DummyPendingManager(
        config=PendingEntryConfig(cancel_on_new_signal=False),
    )
    executor = TradeExecutor(
        trading_module=module,
        pending_entry_manager=pending_manager,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            reentry_cooldown_bars=3,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig(require_armed=True)),
    )

    first = _build_event(spread_points=20.0, close_price=3000.0)
    first = SignalEvent(
        **{
            **first.__dict__,
            "signal_id": "sig_pending_1",
            "metadata": {
                **first.metadata,
                "bar_time": datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            },
        }
    )
    second = _build_event(spread_points=20.0, close_price=3000.0)
    second = SignalEvent(
        **{
            **second.__dict__,
            "signal_id": "sig_pending_2",
            "metadata": {
                **second.metadata,
                "bar_time": datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
            },
            "generated_at": datetime.now(timezone.utc),
        }
    )

    _fire(executor, first)
    _fire(executor, second)

    assert len(module.calls) == 1
    assert len(pending_manager.tracked_orders) == 1
    assert executor._last_entry_bar_time[("XAUUSD", "sma_trend", "buy")] == datetime(
        2026, 1, 1, 0, 0, tzinfo=timezone.utc
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
    event = SignalEvent(**{**event.__dict__, "timeframe": "M5"})

    _fire(executor, event)

    assert module.calls
    payload = module.calls[0][1]
    # M5: sl_atr_mult=1.8, tp_atr_mult=2.5, ATR=2.0
    # SL = 3000 - 1.8*2 = 2996.4, TP = 3000 + 2.5*2 = 3005.0
    assert payload["sl"] == pytest.approx(2996.4)
    assert payload["tp"] == pytest.approx(3005.0)


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


def test_trade_executor_registers_filled_pending_mt5_order_immediately() -> None:
    module = DummyTradingModule()
    module.live_positions = [
        {
            "ticket": 101,
            "symbol": "XAUUSD",
            "comment": "M5:sma_trend:limit_rsig1",
            "price_open": 2999.5,
            "sl": 2995.5,
            "tp": 3007.5,
            "volume": 0.05,
            "time": datetime.now(timezone.utc),
        }
    ]
    position_manager = DummyPositionManager([])
    outcome_tracker = DummyTradeOutcomeTracker()
    executor = TradeExecutor(
        trading_module=module,
        position_manager=position_manager,
        trade_outcome_tracker=outcome_tracker,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig(require_armed=True)),
    )

    result = executor._inspect_pending_mt5_order(
        {
            "signal_id": "sig_1",
            "ticket": 7001,
            "symbol": "XAUUSD",
            "direction": "buy",
            "strategy": "sma_trend",
            "timeframe": "M5",
            "confidence": 0.9,
            "regime": "trend",
            "comment": "M5:sma_trend:limit_rsig1",
            "params": TradeParameters(
                entry_price=3000.0,
                stop_loss=2996.0,
                take_profit=3008.0,
                position_size=0.05,
                risk_reward_ratio=2.0,
                atr_value=2.0,
                sl_distance=4.0,
                tp_distance=8.0,
            ),
        }
    )

    assert result["status"] == "filled"
    assert result["ticket"] == 101
    assert len(position_manager.track_calls) == 1
    assert position_manager.track_calls[0]["ticket"] == 101
    assert position_manager.track_calls[0]["signal_id"] == "sig_1"
    assert outcome_tracker.opened[0]["signal_id"] == "sig_1"
    assert outcome_tracker.opened[0]["fill_price"] == pytest.approx(2999.5)


def test_trade_executor_matches_filled_pending_order_by_strategy_prefix_when_comment_differs() -> None:
    module = DummyTradingModule()
    module.live_positions = [
        {
            "ticket": 202,
            "symbol": "XAUUSD",
            "type": 0,
            "comment": "M30:htf_h4_pullback:limit",
            "price_open": 4649.15,
            "sl": 4583.65,
            "tp": 4731.02,
            "volume": 0.01,
            "time": datetime.now(timezone.utc),
        }
    ]
    position_manager = DummyPositionManager([])
    outcome_tracker = DummyTradeOutcomeTracker()
    executor = TradeExecutor(
        trading_module=module,
        position_manager=position_manager,
        trade_outcome_tracker=outcome_tracker,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig(require_armed=True)),
    )

    result = executor._inspect_pending_mt5_order(
        {
            "signal_id": "sig_h4_pullback",
            "ticket": 7002,
            "symbol": "XAUUSD",
            "direction": "buy",
            "strategy": "htf_h4_pullback",
            "timeframe": "M30",
            "confidence": 0.53,
            "comment": "M30:htf_h4_pullba_r7fab5734",
            "params": TradeParameters(
                entry_price=4661.06,
                stop_loss=4583.65,
                take_profit=4731.02,
                position_size=0.01,
                risk_reward_ratio=1.0,
                atr_value=29.77,
                sl_distance=77.41,
                tp_distance=69.96,
            ),
        }
    )

    assert result["status"] == "filled"
    assert result["ticket"] == 202
    assert len(position_manager.track_calls) == 1
    assert position_manager.track_calls[0]["signal_id"] == "sig_h4_pullback"
    assert outcome_tracker.opened[0]["signal_id"] == "sig_h4_pullback"


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
