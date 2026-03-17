from __future__ import annotations

from src.api.signal import evaluate_signal, list_signal_strategies, recent_signals, signal_summary
from src.api.schemas import SignalEvaluateRequest


class DummySignalService:
    def list_strategies(self):
        return ["rsi_reversion", "sma_trend"]

    def evaluate(self, symbol, timeframe, strategy, indicators=None, metadata=None):
        class _Decision:
            def to_dict(self):
                return {
                    "strategy": strategy,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "action": "buy",
                    "confidence": 0.9,
                    "reason": "unit_test",
                    "used_indicators": ["rsi"],
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "metadata": metadata or {},
                }

        return _Decision()

    def recent_signals(self, **kwargs):
        scope = kwargs.get("scope", "confirmed")
        return [
            {
                "generated_at": "2026-01-01T00:00:00+00:00",
                "signal_id": "abc",
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "strategy": "rsi_reversion",
                "action": "buy",
                "confidence": 0.9,
                "reason": "test",
                "scope": scope,
                "used_indicators": ["rsi"],
                "indicators_snapshot": {"rsi": {"value": 20}},
                "metadata": {},
            }
        ]

    def summary(self, **kwargs):
        scope = kwargs.get("scope", "confirmed")
        return [
            {
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "strategy": "rsi_reversion",
                "action": "buy",
                "count": 3,
                "scope": scope,
                "avg_confidence": 0.82,
                "last_seen_at": "2026-01-01T00:10:00+00:00",
            }
        ]


def test_signal_strategies_endpoint() -> None:
    response = list_signal_strategies(service=DummySignalService())

    assert response.success is True
    assert "sma_trend" in response.data


def test_signal_evaluate_endpoint() -> None:
    response = evaluate_signal(
        SignalEvaluateRequest(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion", metadata={"source": "test"}),
        service=DummySignalService(),
    )

    assert response.success is True
    assert response.data.action == "buy"
    assert response.data.metadata["source"] == "test"


def test_signal_recent_endpoint() -> None:
    response = recent_signals(scope="preview", service=DummySignalService())

    assert response.success is True
    assert response.data[0].signal_id == "abc"
    assert response.data[0].scope == "preview"


def test_signal_summary_endpoint() -> None:
    response = signal_summary(scope="preview", service=DummySignalService())

    assert response.success is True
    assert response.data[0].count == 3
    assert response.data[0].scope == "preview"
