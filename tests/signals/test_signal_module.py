from __future__ import annotations

from src.signals.service import SignalModule


class DummyIndicatorSource:
    def __init__(self):
        self.payload = {
            "sma20": {"sma": 201.0},
            "ema50": {"ema": 200.0},
            "rsi14": {"rsi": 75.0},
        }

    def get_indicator(self, symbol: str, timeframe: str, indicator_name: str):
        return self.payload.get(indicator_name)

    def get_all_indicators(self, symbol: str, timeframe: str):
        return dict(self.payload)

    def list_indicators(self):
        return [{"name": key} for key in self.payload.keys()]


class DummySignalRepository:
    def __init__(self):
        self.confirmed_rows = []
        self.preview_rows = []

    def append(self, record):
        metadata = record.metadata or {}
        target = self.preview_rows if metadata.get("scope") in {"intrabar", "preview"} else self.confirmed_rows
        target.append(record.to_row())

    def recent(self, **kwargs):
        scope = kwargs.get("scope", "confirmed")
        rows = self.preview_rows if scope == "preview" else self.confirmed_rows
        return [
            {
                "generated_at": rows[-1][0].isoformat() if rows[-1][0] else None,
                "signal_id": rows[-1][1],
                "symbol": rows[-1][2],
                "timeframe": rows[-1][3],
                "strategy": rows[-1][4],
                "action": rows[-1][5],
                "confidence": rows[-1][6],
                "reason": rows[-1][7],
                "used_indicators": rows[-1][8],
                "indicators_snapshot": rows[-1][9],
                "metadata": rows[-1][10],
                "scope": scope,
            }
        ] if rows else []

    def summary(self, **kwargs):
        scope = kwargs.get("scope", "confirmed")
        rows = self.preview_rows if scope == "preview" else self.confirmed_rows
        if not rows:
            return []
        return [{
            "symbol": rows[-1][2],
            "timeframe": rows[-1][3],
            "strategy": rows[-1][4],
            "action": rows[-1][5],
            "count": 1,
            "avg_confidence": rows[-1][6],
            "last_seen_at": rows[-1][0].isoformat() if rows[-1][0] else None,
            "scope": scope,
        }]


def test_signal_module_uses_indicator_source_for_default_payload() -> None:
    module = SignalModule(indicator_source=DummyIndicatorSource())

    decision = module.evaluate(symbol="XAUUSD", timeframe="M5", strategy="sma_trend", persist=False)

    assert decision.action == "buy"
    assert decision.strategy == "sma_trend"
    assert "sma20" in decision.used_indicators


def test_signal_module_persists_and_can_query_recent() -> None:
    db = DummySignalRepository()
    module = SignalModule(indicator_source=DummyIndicatorSource(), repository=db)

    module.evaluate(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion")
    recent = module.recent_signals(limit=10)

    assert len(db.confirmed_rows) == 1
    assert recent[0]["strategy"] == "rsi_reversion"
    assert recent[0]["action"] == "sell"
    assert recent[0]["scope"] == "confirmed"


def test_signal_module_dispatch_lists_available_indicators() -> None:
    module = SignalModule(indicator_source=DummyIndicatorSource())

    indicators = module.dispatch_operation("available_indicators")

    assert indicators == ["sma20", "ema50", "rsi14"]


def test_signal_module_exposes_strategy_requirements() -> None:
    module = SignalModule(indicator_source=DummyIndicatorSource())

    requirements = module.dispatch_operation("strategy_requirements")

    assert requirements["sma_trend"] == ["sma20", "ema50"]
    assert requirements["rsi_reversion"] == ["rsi14"]


def test_signal_module_exposes_required_indicator_groups() -> None:
    module = SignalModule(indicator_source=DummyIndicatorSource())

    groups = module.dispatch_operation("required_indicator_groups")

    # 只断言已知的必要条件：结果是列表，且常见单策略的指标组存在于其中。
    # 不断言顺序或总数，因为默认策略列表会随新策略增加而扩展。
    assert isinstance(groups, list)
    assert len(groups) > 0
    flat = {ind for group in groups for ind in group}
    assert "boll20" in flat     # BollingerBreakoutStrategy
    assert "rsi14" in flat      # RsiReversionStrategy
    assert "sma20" in flat      # SmaTrendStrategy / MultiTimeframeConfirmStrategy


def test_signal_module_summary_returns_aggregates() -> None:
    db = DummySignalRepository()
    module = SignalModule(indicator_source=DummyIndicatorSource(), repository=db)

    module.evaluate(symbol="XAUUSD", timeframe="M5", strategy="sma_trend")
    summary = module.summary(hours=24)

    assert summary[0]["count"] == 1
    assert summary[0]["strategy"] == "sma_trend"
    assert summary[0]["scope"] == "confirmed"


def test_bollinger_breakout_strategy_uses_boll20_indicator() -> None:
    """BollingerBreakoutStrategy must look up boll20 (not bollinger20) in the snapshot."""
    # Price below lower band → expect buy signal
    indicators_buy = {
        "boll20": {"bb_upper": 1900.0, "bb_mid": 1890.0, "bb_lower": 1880.0, "close": 1875.0},
    }
    module = SignalModule(indicator_source=DummyIndicatorSource())
    decision = module.evaluate(
        symbol="XAUUSD",
        timeframe="M5",
        strategy="bollinger_breakout",
        indicators=indicators_buy,
        persist=False,
    )
    assert decision.action == "buy", f"expected buy, got {decision.action}: {decision.reason}"
    assert "boll20" in decision.used_indicators

    # Price above upper band → expect sell signal
    indicators_sell = {
        "boll20": {"bb_upper": 1900.0, "bb_mid": 1890.0, "bb_lower": 1880.0, "close": 1910.0},
    }
    decision_sell = module.evaluate(
        symbol="XAUUSD",
        timeframe="M5",
        strategy="bollinger_breakout",
        indicators=indicators_sell,
        persist=False,
    )
    assert decision_sell.action == "sell", f"expected sell, got {decision_sell.action}"


def test_signal_module_rejects_unknown_strategy() -> None:
    module = SignalModule(indicator_source=DummyIndicatorSource())

    try:
        module.evaluate(symbol="XAUUSD", timeframe="M5", strategy="unknown")
        assert False, "expected strategy validation"
    except ValueError as exc:
        assert "unsupported signal strategy" in str(exc)
