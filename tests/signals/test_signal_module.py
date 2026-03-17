from __future__ import annotations

from src.signals.service import SignalModule


class DummyIndicatorSource:
    def __init__(self):
        self.payload = {
            "sma_fast": {"value": 201.0},
            "sma_slow": {"value": 200.0},
            "rsi": {"value": 75.0},
        }

    def get_indicator(self, symbol: str, timeframe: str, indicator_name: str):
        return self.payload.get(indicator_name)

    def get_all_indicators(self, symbol: str, timeframe: str):
        return dict(self.payload)

    def list_indicators(self):
        return [{"name": key} for key in self.payload.keys()]


class DummySignalRepository:
    def __init__(self):
        self.rows = []

    def append(self, record):
        self.rows.append(record.to_row())

    def recent(self, **kwargs):
        return [
            {
                "generated_at": self.rows[-1][0].isoformat() if self.rows[-1][0] else None,
                "signal_id": self.rows[-1][1],
                "symbol": self.rows[-1][2],
                "timeframe": self.rows[-1][3],
                "strategy": self.rows[-1][4],
                "action": self.rows[-1][5],
                "confidence": self.rows[-1][6],
                "reason": self.rows[-1][7],
                "used_indicators": self.rows[-1][8],
                "indicators_snapshot": self.rows[-1][9],
                "metadata": self.rows[-1][10],
            }
        ] if self.rows else []

    def fetch_signal_events(self, **kwargs):
        return [
            (
                self.rows[-1][0],
                self.rows[-1][1],
                self.rows[-1][2],
                self.rows[-1][3],
                self.rows[-1][4],
                self.rows[-1][5],
                self.rows[-1][6],
                self.rows[-1][7],
                self.rows[-1][8],
                self.rows[-1][9],
                self.rows[-1][10],
            )
        ] if self.rows else []

    def summary(self, **kwargs):
        if not self.rows:
            return []
        return [{
            "symbol": self.rows[-1][2],
            "timeframe": self.rows[-1][3],
            "strategy": self.rows[-1][4],
            "action": self.rows[-1][5],
            "count": 1,
            "avg_confidence": self.rows[-1][6],
            "last_seen_at": self.rows[-1][0].isoformat() if self.rows[-1][0] else None,
        }]


def test_signal_module_uses_indicator_source_for_default_payload() -> None:
    module = SignalModule(indicator_source=DummyIndicatorSource())

    decision = module.evaluate(symbol="XAUUSD", timeframe="M5", strategy="sma_trend", persist=False)

    assert decision.action == "buy"
    assert decision.strategy == "sma_trend"
    assert "sma_fast" in decision.used_indicators


def test_signal_module_persists_and_can_query_recent() -> None:
    db = DummySignalRepository()
    module = SignalModule(indicator_source=DummyIndicatorSource(), repository=db)

    module.evaluate(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion")
    recent = module.recent_signals(limit=10)

    assert len(db.rows) == 1
    assert recent[0]["strategy"] == "rsi_reversion"
    assert recent[0]["action"] == "sell"


def test_signal_module_dispatch_lists_available_indicators() -> None:
    module = SignalModule(indicator_source=DummyIndicatorSource())

    indicators = module.dispatch_operation("available_indicators")

    assert indicators == ["sma_fast", "sma_slow", "rsi"]


def test_signal_module_summary_returns_aggregates() -> None:
    db = DummySignalRepository()
    module = SignalModule(indicator_source=DummyIndicatorSource(), repository=db)

    module.evaluate(symbol="XAUUSD", timeframe="M5", strategy="sma_trend")
    summary = module.summary(hours=24)

    assert summary[0]["count"] == 1
    assert summary[0]["strategy"] == "sma_trend"


def test_signal_module_rejects_unknown_strategy() -> None:
    module = SignalModule(indicator_source=DummyIndicatorSource())

    try:
        module.evaluate(symbol="XAUUSD", timeframe="M5", strategy="unknown")
        assert False, "expected strategy validation"
    except ValueError as exc:
        assert "unsupported signal strategy" in str(exc)
