from __future__ import annotations

from src.api.signal import (
    evaluate_signal,
    get_market_structure,
    list_signal_strategies,
    recent_signals,
    signal_monitoring_quality,
    signal_summary,
)
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

    def regime_report(self, **kwargs):
        return {
            "symbol": kwargs["symbol"],
            "timeframe": kwargs["timeframe"],
            "dominant_regime": "trending",
            "probabilities": {"trending": 0.7, "ranging": 0.2, "breakout": 0.05, "uncertain": 0.05},
        }

    def daily_quality_report(self, **kwargs):
        return {
            "symbol": kwargs["symbol"],
            "timeframe": kwargs["timeframe"],
            "total_signals": 12,
            "scope": kwargs.get("scope", "confirmed"),
        }


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


def test_signal_market_structure_endpoint() -> None:
    class DummyAnalyzer:
        def __init__(self) -> None:
            self.calls = []

        def analyze(self, symbol, timeframe, *, event_time=None, latest_close=None):
            self.calls.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "event_time": event_time,
                    "latest_close": latest_close,
                }
            )
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "structure_bias": "bullish_breakout",
            }

    analyzer = DummyAnalyzer()
    response = get_market_structure(
        "XAUUSD",
        "M5",
        analyzer=analyzer,
        market_service=type(
            "MarketServiceStub",
            (),
            {
                "get_quote": lambda _self, symbol=None: type(
                    "QuoteStub",
                    (),
                    {"last": 3000.25, "bid": 3000.20, "ask": 3000.30},
                )()
            },
        )(),
    )

    assert response.success is True
    assert response.data["structure_bias"] == "bullish_breakout"
    assert analyzer.calls[0]["latest_close"] == 3000.25
    assert response.metadata["analysis_mode"] == "live_quote"
    assert response.metadata["price_source"] == "live_quote_last"


def test_signal_market_structure_endpoint_falls_back_without_quote() -> None:
    class DummyAnalyzer:
        def __init__(self) -> None:
            self.calls = []

        def analyze(self, symbol, timeframe, *, event_time=None, latest_close=None):
            self.calls.append({"event_time": event_time, "latest_close": latest_close})
            return {"symbol": symbol, "timeframe": timeframe, "structure_bias": "neutral"}

    analyzer = DummyAnalyzer()
    response = get_market_structure(
        "XAUUSD",
        "M5",
        analyzer=analyzer,
        market_service=type(
            "MarketServiceStub",
            (),
            {"get_quote": lambda _self, symbol=None: None},
        )(),
    )

    assert response.success is True
    assert analyzer.calls[0]["latest_close"] is None
    assert response.metadata["analysis_mode"] == "closed_bar_fallback"
    assert response.metadata["price_source"] == "latest_closed_bar"


def test_signal_monitoring_quality_endpoint() -> None:
    runtime = type("RuntimeStub", (), {"status": lambda self: {"running": True}})()

    response = signal_monitoring_quality(
        "XAUUSD",
        "M5",
        service=DummySignalService(),
        runtime=runtime,
    )

    assert response.success is True
    assert response.data["regime"]["dominant_regime"] == "trending"
    assert response.data["quality"]["total_signals"] == 12
