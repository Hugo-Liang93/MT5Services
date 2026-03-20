from __future__ import annotations

from datetime import datetime, timezone

from src.signals.models import SignalEvent
from src.trading.signal_executor import ExecutorConfig, TradeExecutor


class DummyTradingModule:
    def __init__(self):
        self.calls = []

    def dispatch_operation(self, operation, payload):
        self.calls.append((operation, payload))
        return {"ticket": 1, "payload": payload}

    def account_info(self):
        return {"equity": 10000.0}


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
            require_armed=True,
            min_confidence=0.5,
            sl_atr_multiplier=1.0,
            tp_atr_multiplier=2.0,
            max_spread_to_stop_ratio=0.2,
        ),
    )

    result = executor.on_signal_event(
        _build_event(spread_points=80.0, close_price=3000.0)
    )

    assert result is None
    assert module.calls == []
    assert executor.status()["recent_executions"][-1]["reason"] == "spread_to_stop_ratio_too_high"


def test_trade_executor_records_cost_metrics_on_success() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            require_armed=True,
            min_confidence=0.5,
            sl_atr_multiplier=2.0,
            tp_atr_multiplier=4.0,
            max_spread_to_stop_ratio=0.5,
        ),
    )

    executor.on_signal_event(_build_event(spread_points=50.0, close_price=3000.0))

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
            require_armed=True,
            min_confidence=0.5,
            sl_atr_multiplier=2.0,
            tp_atr_multiplier=4.0,
            max_spread_to_stop_ratio=0.5,
        ),
    )

    executor.on_signal_event(
        _build_event(
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
