from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.signals.evaluation.regime import RegimeType
from src.signals.models import SignalContext, SignalDecision
from src.signals.service import SignalModule
from src.signals.strategies.catalog import build_default_strategy_set
from src.signals.strategies.structured import StructuredTrendContinuation, StructuredSweepReversal


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
        target = (
            self.preview_rows
            if metadata.get("scope") in {"intrabar", "preview"}
            else self.confirmed_rows
        )
        target.append(record.to_row())

    def recent(self, **kwargs):
        scope = kwargs.get("scope", "confirmed")
        rows = self.preview_rows if scope == "preview" else self.confirmed_rows
        return (
            [
                {
                    "generated_at": rows[-1][0].isoformat() if rows[-1][0] else None,
                    "signal_id": rows[-1][1],
                    "symbol": rows[-1][2],
                    "timeframe": rows[-1][3],
                    "strategy": rows[-1][4],
                    "direction": rows[-1][5],
                    "confidence": rows[-1][6],
                    "reason": rows[-1][7],
                    "used_indicators": rows[-1][8],
                    "indicators_snapshot": rows[-1][9],
                    "metadata": rows[-1][10],
                    "scope": scope,
                }
            ]
            if rows
            else []
        )

    def summary(self, **kwargs):
        scope = kwargs.get("scope", "confirmed")
        rows = self.preview_rows if scope == "preview" else self.confirmed_rows
        if not rows:
            return []
        return [
            {
                "symbol": rows[-1][2],
                "timeframe": rows[-1][3],
                "strategy": rows[-1][4],
                "direction": rows[-1][5],
                "count": 1,
                "avg_confidence": rows[-1][6],
                "last_seen_at": rows[-1][0].isoformat() if rows[-1][0] else None,
                "scope": scope,
            }
        ]

    def fetch_winrates(self, **kwargs):
        return [
            {
                "strategy": "sma_trend",
                "direction": "buy",
                "total": 4,
                "wins": 3,
                "win_rate": 0.75,
                "avg_confidence": 0.82,
                "avg_move": 12.5,
            }
        ]

    def fetch_expectancy_stats(self, **kwargs):
        return [
            {
                "strategy": "sma_trend",
                "direction": "buy",
                "total": 4,
                "wins": 3,
                "losses": 1,
                "win_rate": 0.75,
                "avg_win_move": 18.0,
                "avg_loss_move": 7.5,
                "expectancy": 12.25,
                "payoff_ratio": 2.4,
            }
        ]


class DummyDiagnosticsEngine:
    def build_report(self, rows, *, symbol, timeframe, scope, thresholds):
        return {
            "rows_analyzed": len(rows),
            "symbol": symbol,
            "timeframe": timeframe,
            "scope": scope,
            "engine": "dummy",
        }

    def build_daily_quality_report(
        self, rows, *, symbol, timeframe, scope, thresholds, now=None
    ):
        return {
            "rows_analyzed": len(rows),
            "scope": scope,
            "engine": "dummy_daily",
        }


class AffinityProbeStrategy:
    name = "affinity_probe"
    required_indicators = ()
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING: 1.0,
        RegimeType.RANGING: 0.2,
        RegimeType.BREAKOUT: 0.5,
        RegimeType.UNCERTAIN: 0.7,
    }

    def evaluate(self, context: SignalContext) -> SignalDecision:
        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction="buy",
            confidence=0.8,
            reason="probe",
        )


def test_signal_module_uses_indicator_source_for_default_payload() -> None:
    module = SignalModule(
        indicator_source=DummyIndicatorSource(),
        strategies=[AffinityProbeStrategy()],
    )

    decision = module.evaluate(
        symbol="XAUUSD", timeframe="M5", strategy="affinity_probe", persist=False
    )

    assert decision.direction == "buy"
    assert decision.strategy == "affinity_probe"


def test_signal_module_defaults_come_from_shared_strategy_catalog() -> None:
    module = SignalModule(indicator_source=DummyIndicatorSource())

    assert module.list_strategies() == sorted(
        strategy.name for strategy in build_default_strategy_set()
    )


def test_signal_module_persists_and_can_query_recent() -> None:
    db = DummySignalRepository()
    module = SignalModule(
        indicator_source=DummyIndicatorSource(),
        strategies=[AffinityProbeStrategy()],
        repository=db,
    )

    module.evaluate(symbol="XAUUSD", timeframe="M5", strategy="affinity_probe")
    recent = module.recent_signals(limit=10)

    assert len(db.confirmed_rows) == 1
    assert recent[0]["strategy"] == "affinity_probe"
    assert recent[0]["direction"] == "buy"
    assert recent[0]["scope"] == "confirmed"


def test_signal_module_dispatch_lists_available_indicators() -> None:
    module = SignalModule(indicator_source=DummyIndicatorSource())

    indicators = module.dispatch_operation("available_indicators")

    assert indicators == ["sma20", "ema50", "rsi14"]


def test_signal_module_exposes_strategy_requirements() -> None:
    module = SignalModule(indicator_source=DummyIndicatorSource())

    requirements = module.dispatch_operation("strategy_requirements")

    assert "rsi14" in requirements["structured_trend_continuation"]
    assert "atr14" in requirements["structured_trend_continuation"]


def test_signal_module_exposes_required_indicator_groups() -> None:
    module = SignalModule(indicator_source=DummyIndicatorSource())

    groups = module.dispatch_operation("required_indicator_groups")

    # 只断言已知的必要条件：结果是列表，且常见单策略的指标组存在于其中。
    # 不断言顺序或总数，因为默认策略列表会随新策略增加而扩展。
    assert isinstance(groups, list)
    assert len(groups) > 0
    flat = {ind for group in groups for ind in group}
    assert "rsi14" in flat  # StructuredTrendContinuation
    assert "atr14" in flat  # StructuredTrendContinuation


def test_signal_module_summary_returns_aggregates() -> None:
    db = DummySignalRepository()
    module = SignalModule(
        indicator_source=DummyIndicatorSource(),
        strategies=[AffinityProbeStrategy()],
        repository=db,
    )

    module.evaluate(symbol="XAUUSD", timeframe="M5", strategy="affinity_probe")
    summary = module.summary(hours=24)

    assert summary[0]["count"] == 1
    assert summary[0]["strategy"] == "affinity_probe"
    assert summary[0]["scope"] == "confirmed"


def test_signal_module_rejects_unknown_strategy() -> None:
    module = SignalModule(indicator_source=DummyIndicatorSource())

    try:
        module.evaluate(symbol="XAUUSD", timeframe="M5", strategy="unknown")
        assert False, "expected strategy validation"
    except ValueError as exc:
        assert "unsupported signal strategy" in str(exc)


def test_signal_module_uses_weighted_soft_regime_affinity() -> None:
    module = SignalModule(
        indicator_source=DummyIndicatorSource(),
        strategies=[AffinityProbeStrategy()],
        soft_regime_enabled=True,
    )

    decision = module.evaluate(
        symbol="XAUUSD",
        timeframe="M5",
        strategy="affinity_probe",
        indicators={},
        metadata={
            "_soft_regime": {
                "dominant_regime": "uncertain",
                "probabilities": {
                    "trending": 0.4,
                    "ranging": 0.1,
                    "breakout": 0.2,
                    "uncertain": 0.3,
                },
            }
        },
        persist=False,
    )

    expected_affinity = 0.4 * 1.0 + 0.1 * 0.2 + 0.2 * 0.5 + 0.3 * 0.7
    assert decision.confidence == pytest.approx(0.8 * expected_affinity)
    assert decision.metadata["regime_source"] == "soft"
    assert decision.metadata["regime"] == "uncertain"


class DummyDiagnosticRepository:
    def __init__(self, rows):
        self.rows = rows

    def append(self, record):
        return None

    def recent(self, **kwargs):
        scope = kwargs.get("scope", "confirmed")
        if scope == "all":
            return list(self.rows)
        return [r for r in self.rows if r.get("scope") == scope]

    def summary(self, **kwargs):
        return []

    def fetch_expectancy_stats(self, **kwargs):
        return [
            {
                "strategy": "sma_trend",
                "direction": "buy",
                "total": 5,
                "wins": 3,
                "losses": 2,
                "win_rate": 0.6,
                "avg_win_move": 12.0,
                "avg_loss_move": 14.0,
                "expectancy": -1.2,
                "payoff_ratio": 0.86,
            },
            {
                "strategy": "rsi_reversion",
                "direction": "sell",
                "total": 4,
                "wins": 2,
                "losses": 2,
                "win_rate": 0.5,
                "avg_win_move": 8.0,
                "avg_loss_move": 10.0,
                "expectancy": -1.0,
                "payoff_ratio": 0.8,
            },
        ]


def test_strategy_diagnostics_reports_conflicts_and_missing_indicators() -> None:
    rows = [
        {
            "generated_at": "2026-03-19T10:00:00+00:00",
            "signal_id": "1",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "strategy": "sma_trend",
            "direction": "buy",
            "confidence": 0.8,
            "reason": "trend_up",
            "used_indicators": ["sma20", "ema50"],
            "indicators_snapshot": {},
            "metadata": {"bar_time": "2026-03-19T10:00:00+00:00", "regime": "trending"},
            "scope": "confirmed",
        },
        {
            "generated_at": "2026-03-19T10:00:00+00:00",
            "signal_id": "2",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "strategy": "rsi_reversion",
            "direction": "sell",
            "confidence": 0.7,
            "reason": "rsi=76.0",
            "used_indicators": ["rsi14"],
            "indicators_snapshot": {},
            "metadata": {"bar_time": "2026-03-19T10:00:00+00:00", "regime": "trending"},
            "scope": "confirmed",
        },
        {
            "generated_at": "2026-03-19T10:05:00+00:00",
            "signal_id": "3",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "strategy": "rsi_reversion",
            "direction": "hold",
            "confidence": 0.0,
            "reason": "missing_required_indicator:rsi",
            "used_indicators": ["rsi14"],
            "indicators_snapshot": {},
            "metadata": {"bar_time": "2026-03-19T10:05:00+00:00", "regime": "ranging"},
            "scope": "confirmed",
        },
    ]
    module = SignalModule(
        indicator_source=DummyIndicatorSource(),
        repository=DummyDiagnosticRepository(rows),
    )

    report = module.strategy_diagnostics(
        symbol="XAUUSD", timeframe="M5", scope="confirmed", limit=100
    )

    assert report["rows_analyzed"] == 3
    assert report["conflict"]["bars_with_buy_sell_conflict"] == 1
    assert report["conflict"]["bars_with_executable_signals"] == 1
    assert report["dominant_regime"] == "trending"
    assert report["session_distribution"]["london"] == 3
    assert report["thresholds"]["conflict_warn_threshold"] == 0.35
    assert len(report["recommendations"]) >= 1
    assert report["performance_profile"]
    rsi_row = next(
        item
        for item in report["strategy_breakdown"]
        if item["strategy"] == "rsi_reversion"
    )
    assert rsi_row["missing_required_count"] == 1
    assert rsi_row["expectancy"] == -1.0
    rsi_health = next(
        item
        for item in report["strategy_health"]
        if item["strategy"] == "rsi_reversion"
    )
    assert rsi_health["status"] == "warn"
    assert "missing_required_indicator" in rsi_health["warnings"]


def test_signal_module_daily_quality_report() -> None:
    rows = [
        {
            "generated_at": "2026-03-19T10:00:00+00:00",
            "signal_id": "1",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "strategy": "sma_trend",
            "direction": "buy",
            "confidence": 0.8,
            "reason": "trend_up",
            "used_indicators": ["sma20", "ema50"],
            "indicators_snapshot": {},
            "metadata": {"bar_time": "2026-03-19T10:00:00+00:00", "regime": "trending"},
            "scope": "confirmed",
        }
    ]
    module = SignalModule(
        indicator_source=DummyIndicatorSource(),
        repository=DummyDiagnosticRepository(rows),
    )

    report = module.daily_quality_report(
        symbol="XAUUSD",
        timeframe="M5",
        scope="confirmed",
        limit=100,
        now=datetime(2026, 3, 19, 12, 0, tzinfo=timezone.utc),
    )

    assert report["rows_analyzed"] == 1
    assert "window" in report
    assert report["session_distribution"]["london"] == 1
    assert report["performance_profile"]


def test_signal_module_allows_custom_diagnostics_engine_injection() -> None:
    rows = [
        {
            "generated_at": "2026-03-19T10:00:00+00:00",
            "signal_id": "1",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "strategy": "sma_trend",
            "direction": "buy",
            "confidence": 0.8,
            "reason": "trend_up",
            "used_indicators": ["sma20", "ema50"],
            "indicators_snapshot": {},
            "metadata": {"bar_time": "2026-03-19T10:00:00+00:00", "regime": "trending"},
            "scope": "confirmed",
        }
    ]
    module = SignalModule(
        indicator_source=DummyIndicatorSource(),
        repository=DummyDiagnosticRepository(rows),
        diagnostics_engine=DummyDiagnosticsEngine(),
    )

    report = module.strategy_diagnostics(
        symbol="XAUUSD", timeframe="M5", scope="confirmed"
    )
    daily = module.daily_quality_report(
        symbol="XAUUSD", timeframe="M5", scope="confirmed"
    )

    assert report["engine"] == "dummy"
    assert daily["engine"] == "dummy_daily"


def test_signal_module_diagnostics_aggregate_summary_uses_repository_summary() -> None:
    db = DummySignalRepository()
    module = SignalModule(
        indicator_source=DummyIndicatorSource(),
        strategies=[AffinityProbeStrategy()],
        repository=db,
    )
    module.evaluate(symbol="XAUUSD", timeframe="M5", strategy="affinity_probe")
    report = module.diagnostics_aggregate_summary(hours=24, scope="confirmed")

    assert report["source"] == "repository.summary"
    assert report["rows_analyzed"] >= 1
    assert report["direction_totals"]["buy"] >= 1


def test_signal_module_recent_by_trace_id_filters_rows() -> None:
    rows = [
        {
            "generated_at": "2026-03-19T10:00:00+00:00",
            "signal_id": "1",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "strategy": "sma_trend",
            "direction": "buy",
            "confidence": 0.8,
            "reason": "trend_up",
            "used_indicators": ["sma20", "ema50"],
            "indicators_snapshot": {},
            "metadata": {"signal_trace_id": "trace_1"},
            "scope": "confirmed",
        },
        {
            "generated_at": "2026-03-19T10:01:00+00:00",
            "signal_id": "2",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "strategy": "rsi_reversion",
            "direction": "sell",
            "confidence": 0.7,
            "reason": "rsi=72",
            "used_indicators": ["rsi14"],
            "indicators_snapshot": {},
            "metadata": {"signal_trace_id": "trace_2"},
            "scope": "confirmed",
        },
    ]
    module = SignalModule(
        indicator_source=DummyIndicatorSource(),
        repository=DummyDiagnosticRepository(rows),
    )

    matched = module.recent_by_trace_id(
        trace_id="trace_1", scope="confirmed", limit=100
    )

    assert len(matched) == 1
    assert matched[0]["signal_id"] == "1"


class DummyRuntime:
    def get_regime_stability(self, symbol: str, timeframe: str) -> dict:
        return {"symbol": symbol, "timeframe": timeframe, "stable_bars": 3}


def test_signal_module_regime_report_includes_runtime_stability() -> None:
    module = SignalModule(indicator_source=DummyIndicatorSource())

    report = module.regime_report(
        symbol="XAUUSD",
        timeframe="M5",
        runtime=DummyRuntime(),
    )

    assert report["symbol"] == "XAUUSD"
    assert report["timeframe"] == "M5"
    assert report["stability"]["stable_bars"] == 3


def test_signal_module_strategy_winrates_uses_repository() -> None:
    module = SignalModule(
        indicator_source=DummyIndicatorSource(),
        repository=DummySignalRepository(),
    )

    rows = module.strategy_winrates(hours=24, symbol="XAUUSD")

    assert len(rows) == 1
    assert rows[0]["strategy"] == "sma_trend"
    assert rows[0]["win_rate"] == 0.75


def test_signal_module_strategy_expectancy_uses_repository() -> None:
    module = SignalModule(
        indicator_source=DummyIndicatorSource(),
        repository=DummySignalRepository(),
    )

    rows = module.strategy_expectancy(hours=24, symbol="XAUUSD")

    assert len(rows) == 1
    assert rows[0]["strategy"] == "sma_trend"
    assert rows[0]["expectancy"] == 12.25


def test_signal_module_recent_consensus_signals_filters_consensus_strategy() -> None:
    class ConsensusRepository(DummyDiagnosticRepository):
        def recent(self, **kwargs):
            return [
                {
                    "generated_at": "2026-03-19T10:00:00+00:00",
                    "signal_id": "1",
                    "symbol": "XAUUSD",
                    "timeframe": "M5",
                    "strategy": kwargs.get("strategy"),
                    "direction": "buy",
                    "confidence": 0.8,
                    "reason": "consensus",
                    "used_indicators": ["sma20"],
                    "indicators_snapshot": {},
                    "metadata": {},
                    "scope": kwargs.get("scope", "confirmed"),
                }
            ]

    module = SignalModule(
        indicator_source=DummyIndicatorSource(),
        repository=ConsensusRepository([]),
    )

    rows = module.recent_consensus_signals(symbol="XAUUSD", timeframe="M5", limit=10)

    assert len(rows) == 1
    assert rows[0]["strategy"] == "consensus"


def test_intrabar_required_indicators_derives_from_strategy_scopes() -> None:
    """intrabar_required_indicators() 自动推导：仅收集 intrabar 策略的指标。"""
    from src.signals.strategies.structured import StructuredRangeReversion

    module = SignalModule(
        indicator_source=DummyIndicatorSource(),
        strategies=[StructuredRangeReversion(), StructuredTrendContinuation()],
    )
    result = module.intrabar_required_indicators()
    # range_reversion (intrabar+confirmed) → rsi14/atr14 在集合中
    assert "rsi14" in result
    # trend_continuation (confirmed-only) → 其指标不在 intrabar 集合中
    # (但 trend_continuation 也有 rsi14，所以只验证 intrabar 策略的指标存在即可)
