from __future__ import annotations

from datetime import datetime, timezone

from src.signals.models import SignalEvent
from src.market.tick_features.health import TickFeatureHealth
from src.trading.execution.tick_feature_health import build_execution_tick_feature_health
from src.trading.execution.executor import ExecutorConfig, TradeExecutor
from src.trading.execution.gate import ExecutionGate, ExecutionGateConfig
from src.trading.execution.reasons import (
    REASON_TICK_FEATURE_HEALTH_BLOCKED,
    REASON_TICK_FEATURE_HEALTH_UNAVAILABLE,
)


class _TradingModule:
    def __init__(self) -> None:
        self.calls = []

    def dispatch_operation(self, operation, payload):
        self.calls.append((operation, payload))
        return {"ticket": 1}

    def account_info(self):
        return {"equity": 10000.0}

    def get_positions(self, symbol=None):
        return []

    def get_orders(self, symbol=None):
        return []


def _identity():
    return type(
        "Identity",
        (),
        {"environment": "demo", "account_key": "demo", "instance_id": "demo-exec"},
    )()


def _executor(health_fn) -> TradeExecutor:
    return TradeExecutor(
        trading_module=_TradingModule(),
        runtime_identity=_identity(),
        config=ExecutorConfig(enabled=True, min_confidence=0.1),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
        quote_health_fn=lambda _symbol: {"stale": False},
        market_data_health_fn=lambda _symbol: {"status": "healthy", "blocking": False},
        tick_feature_health_fn=health_fn,
    )


def _event(scope: str = "tick_derived") -> SignalEvent:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return SignalEvent(
        symbol="XAUUSD",
        timeframe="M1",
        strategy="tick_probe",
        direction="buy",
        confidence=0.9,
        signal_state="confirmed_buy",
        scope=scope,
        indicators={},
        metadata={"bar_time": now, "snapshot_time": now},
        generated_at=now,
        signal_id=f"sig-{scope}",
        reason="test",
    )


def test_tick_derived_event_rejects_missing_tick_feature_health() -> None:
    executor = _executor(lambda _symbol: {"status": "missing", "blocking": True})

    result = executor.process_event(_event())

    assert result["status"] == "skipped"
    assert result["reason"] == REASON_TICK_FEATURE_HEALTH_UNAVAILABLE


def test_tick_derived_event_rejects_blocked_tick_feature_health() -> None:
    executor = _executor(lambda _symbol: {"status": "blocked", "blocking": True})

    result = executor.process_event(_event())

    assert result["status"] == "skipped"
    assert result["reason"] == REASON_TICK_FEATURE_HEALTH_BLOCKED


def test_confirmed_event_does_not_require_tick_feature_health() -> None:
    calls = {"count": 0}

    def health(_symbol):
        calls["count"] += 1
        return {"status": "blocked", "blocking": True}

    executor = _executor(health)

    executor.process_event(_event("confirmed"))

    assert calls["count"] == 0


def test_execution_tick_feature_health_uses_public_projection_port() -> None:
    class Store:
        def health_for(self, symbol, *, bus_stats=None):
            return TickFeatureHealth(
                symbol=symbol,
                status="healthy",
                last_snapshot_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                last_window_end_msc=1_767_225_600_000,
                snapshot_age_seconds=1.0,
                queue_depth=0,
                dropped_snapshots=0,
                last_reasons=(),
            )

        def health_payload(self, symbol, *, bus_stats=None):
            return self.health_for(symbol, bus_stats=bus_stats).to_dict()

        def _health_to_dict(self, health):  # pragma: no cover - must not be called
            raise AssertionError("private health projection must not be used")

    payload = build_execution_tick_feature_health(Store(), None, "XAUUSD")

    assert payload["status"] == "healthy"
    assert payload["blocking"] is False
