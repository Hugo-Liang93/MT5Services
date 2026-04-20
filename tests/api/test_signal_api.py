from __future__ import annotations

from src.api.schemas import SignalEvaluateRequest
from src.api.signal import (
    evaluate_signal,
    get_intrabar_slos_timeseries,
    get_market_structure,
    get_tracked_positions,
    list_signal_strategies,
    recent_signals,
    signal_monitoring_quality,
    signal_runtime_status,
    signal_strategy_audit,
    signal_summary,
)
from src.readmodels.runtime import RuntimeReadModel


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
            "probabilities": {
                "trending": 0.7,
                "ranging": 0.2,
                "breakout": 0.05,
                "uncertain": 0.05,
            },
        }

    def daily_quality_report(self, **kwargs):
        return {
            "symbol": kwargs["symbol"],
            "timeframe": kwargs["timeframe"],
            "total_signals": 12,
            "scope": kwargs.get("scope", "confirmed"),
        }


class PagedSignalService(DummySignalService):
    def __init__(self) -> None:
        self.last_kwargs = {}

    def recent_signal_page(self, **kwargs):
        self.last_kwargs = dict(kwargs)
        return {
            "items": [
                {
                    "generated_at": "2026-01-02T00:00:00+00:00",
                    "signal_id": "page_1",
                    "symbol": kwargs.get("symbol") or "XAUUSD",
                    "timeframe": kwargs.get("timeframe") or "M5",
                    "strategy": kwargs.get("strategy") or "rsi_reversion",
                    "direction": kwargs.get("direction") or "sell",
                    "confidence": 0.77,
                    "reason": "paged",
                    "scope": kwargs.get("scope") or "confirmed",
                    "used_indicators": ["rsi"],
                    "indicators_snapshot": {"rsi": {"value": 72}},
                    "metadata": {
                        "signal_state": kwargs.get("status") or "confirmed_ready"
                    },
                }
            ],
            "total": 42,
            "page": kwargs.get("page") or 1,
            "page_size": kwargs.get("page_size") or 10,
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


class DummyHealthMonitor:
    def __init__(self, samples: dict[tuple[str, str], list[dict]]) -> None:
        self._samples = samples

    def get_recent_metrics(
        self, component: str, metric_name: str, limit: int
    ) -> list[dict]:
        return self._samples.get((component, metric_name), [])[:limit]


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
        SignalEvaluateRequest(
            symbol="XAUUSD",
            timeframe="M5",
            strategy="rsi_reversion",
            metadata={"source": "test"},
        ),
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


def test_signal_recent_endpoint_exposes_pagination_filters() -> None:
    service = PagedSignalService()
    response = recent_signals(
        symbol="XAUUSD",
        timeframe="M15",
        strategy="rsi_reversion",
        direction="sell",
        status="confirmed_ready",
        scope="confirmed",
        page=2,
        page_size=25,
        sort="generated_at_asc",
        service=service,
    )

    assert response.success is True
    assert response.data[0].signal_id == "page_1"
    assert response.metadata["page"] == 2
    assert response.metadata["page_size"] == 25
    assert response.metadata["total"] == 42
    assert response.metadata["direction"] == "sell"
    assert response.metadata["status"] == "confirmed_ready"
    assert response.metadata["sort"] == "generated_at_asc"
    assert service.last_kwargs["page"] == 2
    assert service.last_kwargs["page_size"] == 25


def test_signal_recent_endpoint_propagates_actionability_filter() -> None:
    """P9 Phase 1.5: actionability 参数透传到 service.recent_signal_page。"""
    service = PagedSignalService()

    response = recent_signals(actionability="blocked", service=service)

    assert response.success is True
    assert service.last_kwargs["actionability"] == "blocked"
    assert response.metadata["actionability"] == "blocked"


def test_signal_recent_endpoint_supports_priority_sort() -> None:
    """P9 Phase 1.5: 新增 priority_desc / priority_asc sort 选项。"""
    service = PagedSignalService()

    response = recent_signals(sort="priority_desc", service=service)

    assert response.success is True
    assert service.last_kwargs["sort"] == "priority_desc"
    assert response.metadata["sort"] == "priority_desc"


def test_signal_recent_endpoint_returns_admission_fields_when_present() -> None:
    """P9 Phase 1.5: payload 含 actionability/guard_*/priority/rank_source 时，
    SignalEventModel 应原样透传，不报错。"""

    class _AdmissionAwareService(DummySignalService):
        def recent_signal_page(self, **kwargs):
            return {
                "items": [
                    {
                        "generated_at": "2026-04-20T10:00:00+00:00",
                        "signal_id": "sig_b",
                        "symbol": "XAUUSD",
                        "timeframe": "H1",
                        "strategy": "trend",
                        "direction": "buy",
                        "confidence": 0.82,
                        "reason": "test",
                        "scope": "confirmed",
                        "used_indicators": [],
                        "indicators_snapshot": {},
                        "metadata": {},
                        "actionability": "blocked",
                        "guard_reason_code": "spread_to_stop_ratio_too_high",
                        "guard_category": "cost_guard",
                        "priority": 0.082,
                        "rank_source": "native",
                    }
                ],
                "total": 1,
                "page": 1,
                "page_size": 10,
            }

    response = recent_signals(service=_AdmissionAwareService())

    assert response.success is True
    item = response.data[0]
    assert item.actionability == "blocked"
    assert item.guard_reason_code == "spread_to_stop_ratio_too_high"
    assert item.guard_category == "cost_guard"
    assert item.priority == 0.082
    assert item.rank_source == "native"


def test_signal_recent_endpoint_keeps_admission_fields_none_for_legacy_records() -> (
    None
):
    """旧记录（DB ALTER TABLE 之前）不带 admission 字段，API 必须返回 None。"""
    response = recent_signals(scope="preview", service=DummySignalService())

    assert response.success is True
    item = response.data[0]
    # DummySignalService.recent_signals 不返回 admission 字段，模型默认 None
    assert item.actionability is None
    assert item.guard_reason_code is None
    assert item.priority is None


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
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "structure_bias": "neutral",
            }

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


def test_signal_intrabar_slos_timeseries_endpoint() -> None:
    health_monitor = DummyHealthMonitor(
        {
            ("indicator_calculation", "intrabar_drop_rate_1m"): [
                {"timestamp": "t1", "value": 0.8, "alert_level": "healthy"},
                {"timestamp": "t2", "value": 1.2, "alert_level": "warning"},
            ],
            ("indicator_calculation", "intrabar_queue_age_p95_ms"): [
                {"timestamp": "t2", "value": 2400.0, "alert_level": "warning"},
            ],
            ("indicator_calculation", "intrabar_to_decision_latency_p95_ms"): [
                {"timestamp": "t2", "value": 8000.0, "alert_level": "critical"},
            ],
        }
    )

    response = get_intrabar_slos_timeseries(
        component="indicator_calculation",
        limit=10,
        health_monitor=health_monitor,
    )

    assert response.success is True
    assert response.data["component"] == "indicator_calculation"
    assert response.data["limit"] == 10
    assert response.data["drop_rate"][1]["alert_level"] == "warning"
    assert response.data["queue_age_ms_p95"][0]["value"] == 2400.0
    assert response.data["to_decision_latency_ms_p95"][0]["alert_level"] == "critical"


# ── /v1/signals/diagnostics/strategy-audit (backlog P0.3) ─────────


class _StrategyAuditService:
    """符合 SignalModule 接口的 stub，仅暴露 strategy_audit() 必需方法。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.last_kwargs: dict | None = None

    def strategy_audit(self, **kwargs):
        self.last_kwargs = dict(kwargs)
        return self._payload


def test_signal_strategy_audit_endpoint_returns_payload_and_metadata() -> None:
    payload = {
        "rows_analyzed": 12,
        "scope": "confirmed",
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "thresholds": {
            "conflict_warn_threshold": 0.35,
            "hold_warn_threshold": 0.75,
            "confidence_warn_threshold": 0.45,
        },
        "strategies": [
            {
                "strategy": "trend",
                "category": "trend",
                "signals": 8,
                "actionable_signals": 5,
                "hold_count": 2,
                "blocked_count": 1,
                "conflict_count": 0,
                "hold_rate": 0.25,
                "blocked_rate": 0.125,
                "conflict_rate": 0.0,
                "avg_confidence": 0.72,
                "win_rate": 0.6,
                "last_signal_at": "2026-04-20T10:00:00+00:00",
                "recent_issue": None,
                "status": "ok",
                "warnings": [],
            },
            {
                "strategy": "reversion",
                "category": "reversion",
                "signals": 4,
                "actionable_signals": 0,
                "hold_count": 4,
                "blocked_count": 0,
                "conflict_count": 0,
                "hold_rate": 1.0,
                "blocked_rate": 0.0,
                "conflict_rate": 0.0,
                "avg_confidence": 0.4,
                "win_rate": None,
                "last_signal_at": "2026-04-20T09:00:00+00:00",
                "recent_issue": "high_hold_ratio",
                "status": "warn",
                "warnings": ["high_hold_ratio", "low_avg_confidence"],
            },
        ],
    }
    service = _StrategyAuditService(payload)

    response = signal_strategy_audit(
        symbol="XAUUSD",
        timeframe="H1",
        scope="confirmed",
        service=service,
    )

    assert response.success is True
    assert response.data["rows_analyzed"] == 12
    assert len(response.data["strategies"]) == 2
    assert response.data["strategies"][0]["strategy"] == "trend"
    assert response.data["strategies"][0]["win_rate"] == 0.6
    assert response.data["strategies"][1]["status"] == "warn"
    assert response.data["strategies"][1]["recent_issue"] == "high_hold_ratio"
    assert response.metadata["operation"] == "signal_strategy_audit"
    assert response.metadata["strategy_count"] == 2
    assert response.metadata["rows_analyzed"] == 12
    # 透传过滤参数到 service 层
    assert service.last_kwargs["symbol"] == "XAUUSD"
    assert service.last_kwargs["scope"] == "confirmed"


def test_signal_strategy_audit_endpoint_propagates_strategy_filter() -> None:
    service = _StrategyAuditService(
        payload={
            "rows_analyzed": 0,
            "scope": "confirmed",
            "symbol": None,
            "timeframe": None,
            "thresholds": {},
            "strategies": [],
        }
    )

    response = signal_strategy_audit(strategy="trend", service=service)

    assert response.success is True
    assert service.last_kwargs["strategy"] == "trend"
    assert response.metadata["strategy"] == "trend"


def test_signal_strategy_audit_endpoint_passes_threshold_overrides() -> None:
    service = _StrategyAuditService(payload={"rows_analyzed": 0, "strategies": []})

    signal_strategy_audit(
        hold_warn_threshold=0.5,
        confidence_warn_threshold=0.3,
        service=service,
    )

    assert service.last_kwargs["hold_warn_threshold"] == 0.5
    assert service.last_kwargs["confidence_warn_threshold"] == 0.3
