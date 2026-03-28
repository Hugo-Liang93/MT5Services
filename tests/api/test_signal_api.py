from __future__ import annotations

from src.api.signal import (
    evaluate_signal,
    get_tracked_positions,
    get_market_structure,
    list_signal_strategies,
    recent_signals,
    signal_monitoring_quality,
    signal_runtime_status,
    signal_summary,
)
from src.readmodels.runtime import RuntimeReadModel
from src.api.schemas import SignalEvaluateRequest


class DummySignalService:
    def strategy_catalog(self):
        return [
            {
                "name": "rsi_reversion",
                "category": "reversion",
                "preferred_scopes": ["confirmed"],
                "required_indicators": ["rsi14"],
                "regime_affinity": {},
            },
            {
                "name": "sma_trend",
                "category": "trend",
                "preferred_scopes": ["confirmed"],
                "required_indicators": ["sma20", "ema50"],
                "regime_affinity": {},
            },
        ]

    def list_strategies(self):
        return ["rsi_reversion", "sma_trend"]

    def strategy_category(self, strategy):
        return "reversion" if "rsi" in strategy else "trend"

    def strategy_scopes(self, strategy):
        return ("confirmed",)

    def strategy_requirements(self, strategy):
        return ("rsi14",) if "rsi" in strategy else ("sma20", "ema50")

    def strategy_affinity_map(self, strategy):
        return {}

    def evaluate(self, symbol, timeframe, strategy, indicators=None, metadata=None):
        class _Decision:
            def to_dict(self):
                return {
                    "strategy": strategy,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "direction": "buy",
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
                "direction": "buy",
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
                "direction": "buy",
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


class DummySignalRuntime:
    def status(self):
        return {
            "running": True,
            "target_count": 1,
            "confirmed_queue_size": 2,
            "confirmed_queue_capacity": 32,
            "intrabar_queue_size": 1,
            "intrabar_queue_capacity": 16,
            "processed_events": 10,
            "dropped_events": 0,
            "confirmed_backpressure_failures": 0,
            "warmup_ready": True,
        }


class DummyPositionManager:
    def status(self):
        return {
            "running": True,
            "tracked_positions": 1,
            "reconcile_count": 3,
        }

    def active_positions(self):
        return [{"ticket": 1, "symbol": "XAUUSD"}]


def test_signal_strategies_endpoint() -> None:
    response = list_signal_strategies(service=DummySignalService())

    assert response.success is True
    names = [s["name"] for s in response.data]
    assert "sma_trend" in names
    # Verify enriched fields
    rsi = next(s for s in response.data if s["name"] == "rsi_reversion")
    assert rsi["category"] == "reversion"
    assert "rsi14" in rsi["required_indicators"]


def test_signal_evaluate_endpoint() -> None:
    response = evaluate_signal(
        SignalEvaluateRequest(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion", metadata={"source": "test"}),
        service=DummySignalService(),
    )

    assert response.success is True
    assert response.data.direction == "buy"
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


def test_signal_runtime_status_endpoint_uses_runtime_projection() -> None:
    response = signal_runtime_status(
        runtime_views=RuntimeReadModel(signal_runtime=DummySignalRuntime())
    )

    assert response.success is True
    assert response.data["status"] == "healthy"
    assert response.data["queues"]["confirmed"]["size"] == 2


def test_signal_positions_endpoint_uses_runtime_projection() -> None:
    response = get_tracked_positions(
        runtime_views=RuntimeReadModel(position_manager=DummyPositionManager())
    )

    assert response.success is True
    assert response.data["count"] == 1
    assert response.data["items"][0]["symbol"] == "XAUUSD"
    assert response.metadata["position_manager_status"] == "healthy"
