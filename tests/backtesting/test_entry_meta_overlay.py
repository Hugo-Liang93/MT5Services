from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.backtesting.engine.runner import BacktestEngine
from src.backtesting.engine.signals import process_decision
from src.clients.mt5_market import OHLC
from src.research.entry_meta.artifacts import EntryMetaArtifact, EntryMetaPrediction
from src.research.entry_meta.overlay import EntryMetaBacktestOverlay
from src.signals.models import SignalDecision


def _artifact(*, status: str = "accepted") -> EntryMetaArtifact:
    return EntryMetaArtifact(
        model_id="entry-meta-test",
        symbol="XAUUSD",
        timeframe="H1",
        backend="cpu",
        model_kind="logistic",
        feature_keys=[],
        label_summary={"take": 1, "block": 1},
        sample_weight_summary={},
        metrics={},
        predictions=[
            EntryMetaPrediction(
                bar_time="2026-01-01T00:00:00Z",
                strategy="breakout",
                direction="buy",
                take_entry_prob=0.72,
                block_entry_prob=0.28,
            ),
            EntryMetaPrediction(
                bar_time="2026-01-01T01:00:00+00:00",
                strategy="breakout",
                direction="buy",
                take_entry_prob=0.42,
                block_entry_prob=0.58,
            ),
        ],
        model_payload={},
        feature_manifest={},
        status=status,
    )


def _dynamic_artifact(*, status: str = "accepted") -> EntryMetaArtifact:
    feature_keys = [
        "entry.confidence",
        "entry.direction.buy",
        "entry.direction.sell",
        "entry.price",
        "entry.strategy_code",
        "indicator.atr14.atr",
        "matrix.regime_code",
        "matrix.session_code",
    ]
    return EntryMetaArtifact(
        model_id="entry-meta-dynamic",
        symbol="XAUUSD",
        timeframe="H1",
        backend="cpu",
        model_kind="tabular",
        feature_keys=feature_keys,
        label_summary={"take_entry": 2, "block_entry": 2},
        sample_weight_summary={},
        metrics={},
        predictions=[],
        model_payload={
            "estimator": "logistic_regression_v1",
            "feature_order": feature_keys,
            "classes": [0, 1],
            "coef": [[-10.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]],
            "intercept": [5.0],
            "normalization": {
                "mean": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                "scale": [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
            },
            "prediction_reuse": "dynamic_scorer",
        },
        feature_manifest={
            "feature_scope": "runtime_safe",
            "dynamic_scoring_supported": True,
            "runtime_indicator_names": ["atr14"],
            "category_mappings": {
                "strategy": {"breakout": 0.0},
                "regime": {"trend": 0.0},
                "session": {"london": 0.0},
            },
        },
        status=status,
    )


def _research_full_artifact(*, status: str = "accepted") -> EntryMetaArtifact:
    return dataclasses.replace(
        _dynamic_artifact(status=status),
        model_id="entry-meta-research-full",
        feature_manifest={
            "feature_scope": "research_full",
            "dynamic_scoring_supported": False,
            "category_mappings": {
                "strategy": {"breakout": 0.0},
                "regime": {"trend": 0.0},
                "session": {"london": 0.0},
            },
        },
    )


def _feature_context(confidence: float = 0.9):
    from src.research.entry_meta.features import EntryMetaFeatureContext

    return EntryMetaFeatureContext(
        bar_time=datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
        bar_index=3,
        strategy="breakout",
        direction="buy",
        confidence=confidence,
        entry_price=1.25,
        indicators={"atr14": {"atr": 0.01}},
        regime="trend",
        session="london",
    )


def test_shadow_mode_records_without_blocking() -> None:
    overlay = EntryMetaBacktestOverlay(
        _artifact(status="refit"), mode="shadow", threshold=0.65
    )

    verdict = overlay.evaluate(
        datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        "breakout",
        "buy",
        confidence=0.8,
    )

    assert verdict.allowed is True
    assert verdict.reason == "allowed"
    assert verdict.take_entry_prob == 0.42
    assert verdict.score_source == "artifact_prediction"
    report = overlay.report()
    assert report["artifact_status"] == "refit"
    assert report["observed"] == 1
    assert report["allowed"] == 1
    assert report["blocked"] == 0
    assert report["missing_predictions"] == 0
    assert report["score_source_counts"] == {"artifact_prediction": 1}
    assert report["probability_summary"] == {
        "matched_predictions": 1,
        "take_entry_prob_min": 0.42,
        "take_entry_prob_max": 0.42,
        "take_entry_prob_avg": 0.42,
        "block_entry_prob_min": 0.58,
        "block_entry_prob_max": 0.58,
        "block_entry_prob_avg": 0.58,
        "take_entry_prob_below_threshold": 1,
        "take_entry_prob_at_or_above_threshold": 0,
    }


def test_filter_mode_blocks_when_take_probability_below_threshold() -> None:
    overlay = EntryMetaBacktestOverlay(_artifact(), mode="filter", threshold=0.65)

    verdict = overlay.evaluate(
        "2026-01-01T01:00:00Z",
        "breakout",
        "buy",
        confidence=0.8,
    )

    assert verdict.allowed is False
    assert verdict.reason == "entry_meta_probability_below_threshold"
    assert verdict.take_entry_prob == 0.42
    assert verdict.block_entry_prob == 0.58
    assert overlay.report()["blocked_by_strategy"] == {"breakout": 1}


def test_filter_mode_allows_accepted_artifact_when_probability_meets_threshold() -> (
    None
):
    overlay = EntryMetaBacktestOverlay(_artifact(), mode="filter", threshold=0.65)

    verdict = overlay.evaluate(
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        "breakout",
        "buy",
        confidence=0.8,
    )

    assert verdict.allowed is True
    assert verdict.reason == "allowed"
    assert overlay.report()["allowed"] == 1


def test_missing_prediction_is_allowed_and_counted() -> None:
    overlay = EntryMetaBacktestOverlay(_artifact(), mode="filter", threshold=0.65)

    verdict = overlay.evaluate(
        datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
        "breakout",
        "buy",
        confidence=0.8,
    )

    assert verdict.allowed is True
    assert verdict.reason == "entry_meta_dynamic_feature_scope_unsupported"
    assert verdict.take_entry_prob == 1.0
    assert verdict.block_entry_prob == 0.0
    assert verdict.score_source == "missing"
    report = overlay.report()
    assert report["observed"] == 1
    assert report["allowed"] == 1
    assert report["missing_predictions"] == 1
    assert report["missing_by_reason"] == {
        "entry_meta_dynamic_feature_scope_unsupported": 1
    }
    assert report["score_source_counts"] == {"missing": 1}
    assert report["dynamic_score_failures"] == 1


def test_overlay_uses_dynamic_scorer_when_prediction_lookup_misses() -> None:
    overlay = EntryMetaBacktestOverlay(
        _dynamic_artifact(), mode="filter", threshold=0.50
    )

    verdict = overlay.evaluate(
        "2026-01-01T03:00:00Z",
        "breakout",
        "buy",
        confidence=0.9,
        feature_context=_feature_context(confidence=0.9),
    )

    assert verdict.allowed is False
    assert verdict.reason == "entry_meta_probability_below_threshold"
    assert verdict.prediction is None
    assert verdict.score_source == "dynamic_scorer"
    report = overlay.report()
    assert report["score_source_counts"]["dynamic_scorer"] == 1
    assert report["dynamic_scored"] == 1
    assert report["missing_predictions"] == 0
    assert report["feature_scope"] == "runtime_safe"
    assert report["dynamic_scoring_supported"] is True


def test_overlay_research_full_lookup_miss_is_allowed_and_reports_scope() -> None:
    overlay = EntryMetaBacktestOverlay(
        _research_full_artifact(), mode="filter", threshold=0.50
    )

    verdict = overlay.evaluate(
        "2026-01-01T03:00:00Z",
        "breakout",
        "buy",
        confidence=0.9,
        feature_context=_feature_context(confidence=0.9),
    )

    assert verdict.allowed is True
    assert verdict.reason == "entry_meta_dynamic_feature_scope_unsupported"
    assert verdict.score_source == "missing"
    report = overlay.report()
    assert report["feature_scope"] == "research_full"
    assert report["dynamic_scoring_supported"] is False
    assert report["missing_by_reason"] == {
        "entry_meta_dynamic_feature_scope_unsupported": 1
    }
    assert report["dynamic_scored"] == 0
    assert report["dynamic_score_failures"] == 1


def test_overlay_research_full_lookup_miss_without_context_reports_scope() -> None:
    overlay = EntryMetaBacktestOverlay(
        _research_full_artifact(),
        mode="filter",
        threshold=0.50,
    )

    verdict = overlay.evaluate(
        "2026-01-01T03:00:00Z",
        "breakout",
        "buy",
        confidence=0.9,
    )

    assert verdict.allowed is True
    assert verdict.reason == "entry_meta_dynamic_feature_scope_unsupported"
    assert verdict.score_source == "missing"
    assert overlay.report()["missing_by_reason"] == {
        "entry_meta_dynamic_feature_scope_unsupported": 1
    }


def test_overlay_rejects_inconsistent_dynamic_scope_manifest() -> None:
    artifact = dataclasses.replace(
        _dynamic_artifact(),
        feature_manifest={
            "feature_scope": "research_full",
            "dynamic_scoring_supported": True,
            "category_mappings": {
                "strategy": {"breakout": 0.0},
                "regime": {"trend": 0.0},
                "session": {"london": 0.0},
            },
        },
    )

    with pytest.raises(ValueError, match="dynamic_scoring_supported"):
        EntryMetaBacktestOverlay(artifact, mode="filter", threshold=0.50)


def test_overlay_rejects_invalid_dynamic_scorer_feature_order() -> None:
    artifact = _dynamic_artifact()
    artifact.model_payload["feature_order"] = list(reversed(artifact.feature_keys))

    with pytest.raises(
        ValueError,
        match="dynamic scorer|feature_order|invalid artifact",
    ):
        EntryMetaBacktestOverlay(artifact, mode="filter", threshold=0.50)


def test_overlay_dynamic_feature_failure_allows_and_records_reason() -> None:
    overlay = EntryMetaBacktestOverlay(
        _dynamic_artifact(), mode="filter", threshold=0.50
    )
    bad_context = dataclasses.replace(
        _feature_context(confidence=0.9), strategy="unknown"
    )

    verdict = overlay.evaluate(
        "2026-01-01T03:00:00Z",
        "unknown",
        "buy",
        confidence=0.9,
        feature_context=bad_context,
    )

    assert verdict.allowed is True
    assert verdict.reason == "entry_meta_unknown_category"
    assert verdict.score_source == "missing"
    report = overlay.report()
    assert report["missing_by_reason"]["entry_meta_unknown_category"] == 1
    assert report["dynamic_score_failures"] == 1


def test_overlay_dynamic_missing_feature_allows_and_records_reason() -> None:
    overlay = EntryMetaBacktestOverlay(
        _dynamic_artifact(), mode="filter", threshold=0.50
    )
    bad_context = dataclasses.replace(_feature_context(confidence=0.9), indicators={})

    verdict = overlay.evaluate(
        "2026-01-01T03:00:00Z",
        "breakout",
        "buy",
        confidence=0.9,
        feature_context=bad_context,
    )

    assert verdict.allowed is True
    assert verdict.reason == "entry_meta_feature_missing"
    assert verdict.score_source == "missing"
    report = overlay.report()
    assert report["missing_by_reason"]["entry_meta_feature_missing"] == 1
    assert report["dynamic_score_failures"] == 1


def test_filter_mode_rejects_non_accepted_artifact_status_at_construction() -> None:
    with pytest.raises(ValueError, match="status.*accepted"):
        EntryMetaBacktestOverlay(_artifact(status="refit"), mode="filter")


def test_process_decision_records_entry_meta_blocked_event_before_entry() -> None:
    overlay = EntryMetaBacktestOverlay(_artifact(), mode="filter", threshold=0.65)
    rejections: list[str] = []
    blocked_events: list[dict] = []
    engine = _ProcessDecisionEngine(
        state_edge_overlay=None,
        entry_meta_overlay=overlay,
        rejections=rejections,
        blocked_events=blocked_events,
    )
    decision = SignalDecision(
        strategy="breakout",
        symbol="XAUUSD",
        timeframe="H1",
        direction="buy",
        confidence=0.9,
        reason="test",
    )
    bar = OHLC(
        symbol="XAUUSD",
        timeframe="H1",
        time=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        open=1.0,
        high=1.0,
        low=1.0,
        close=1.0,
        volume=1.0,
    )

    process_decision(engine, decision, bar, 1, {"atr14": {"atr": 1.0}}, "unknown")

    assert rejections == ["entry_meta_filter"]
    assert blocked_events == [
        {
            "source": "entry_meta_overlay",
            "execution_reason": "entry_meta_filter",
            "reason": "entry_meta_probability_below_threshold",
            "bar_time": "2026-01-01T01:00:00+00:00",
            "bar_index": 1,
            "strategy": "breakout",
            "direction": "buy",
            "confidence": 0.9,
            "regime": "unknown",
            "price": 1.0,
            "take_entry_prob": 0.42,
            "block_entry_prob": 0.58,
            "threshold": 0.65,
        }
    ]
    assert overlay.report()["blocked"] == 1


def test_process_decision_passes_entry_meta_feature_context() -> None:
    captured: list[object] = []
    entry_meta_overlay = _EntryMetaOverlayCapturingContext(captured)
    engine = _ProcessDecisionEngine(
        state_edge_overlay=None,
        entry_meta_overlay=entry_meta_overlay,
        rejections=[],
        blocked_events=[],
    )

    process_decision(
        engine,
        _decision(),
        _bar(),
        1,
        {"atr14": {"atr": 0.5}},
        "trend",
        entry_session="london",
    )

    context = captured[0]
    assert context.bar_time == _bar().time
    assert context.bar_index == 1
    assert context.strategy == "breakout"
    assert context.direction == "buy"
    assert context.confidence == 0.9
    assert context.entry_price == 1.0
    assert context.indicators == {"atr14": {"atr": 0.5}}
    assert context.regime == "trend"
    assert context.session == "london"


def test_backtest_engine_entry_meta_overlay_report_uses_public_overlay_report() -> None:
    overlay = EntryMetaBacktestOverlay(_artifact(), mode="shadow")
    engine = BacktestEngine.__new__(BacktestEngine)
    engine._entry_meta_overlay = overlay

    assert engine.entry_meta_overlay is overlay
    assert engine.entry_meta_overlay_report() == overlay.report()


def test_backtest_engine_state_edge_overlay_uses_public_read_only_port() -> None:
    overlay = SimpleNamespace(report=lambda: {"overlay": "state-edge"})
    engine = BacktestEngine.__new__(BacktestEngine)
    engine._state_edge_overlay = overlay

    assert engine.state_edge_overlay is overlay
    assert engine.state_edge_overlay_report() == {"overlay": "state-edge"}


def test_state_edge_block_prevents_entry_meta_overlay_call() -> None:
    state_edge_overlay = _StateEdgeOverlayStub(allowed=False)
    entry_meta_overlay = _EntryMetaOverlayStub(allowed=False)
    rejections: list[str] = []
    blocked_events: list[dict] = []
    engine = _ProcessDecisionEngine(
        state_edge_overlay=state_edge_overlay,
        entry_meta_overlay=entry_meta_overlay,
        rejections=rejections,
        blocked_events=blocked_events,
    )

    process_decision(
        engine,
        _decision(),
        _bar(),
        1,
        {"atr14": {"atr": 1.0}},
        "unknown",
    )

    assert rejections == ["state_edge_filter"]
    assert entry_meta_overlay.calls == 0
    assert blocked_events[0]["source"] == "state_edge_overlay"


def test_entry_meta_filter_can_block_after_state_edge_allows() -> None:
    state_edge_overlay = _StateEdgeOverlayStub(allowed=True)
    entry_meta_overlay = EntryMetaBacktestOverlay(
        _artifact(), mode="filter", threshold=0.65
    )
    rejections: list[str] = []
    blocked_events: list[dict] = []
    engine = _ProcessDecisionEngine(
        state_edge_overlay=state_edge_overlay,
        entry_meta_overlay=entry_meta_overlay,
        rejections=rejections,
        blocked_events=blocked_events,
    )

    process_decision(
        engine,
        _decision(),
        _bar(),
        1,
        {"atr14": {"atr": 1.0}},
        "unknown",
    )

    assert state_edge_overlay.calls == 1
    assert rejections == ["entry_meta_filter"]
    assert blocked_events[0]["source"] == "entry_meta_overlay"


def test_process_decision_requires_public_entry_meta_overlay_port() -> None:
    engine = _NoEntryMetaOverlayPortEngine()

    with pytest.raises(AttributeError):
        process_decision(
            engine,
            _decision(),
            _bar(),
            1,
            {"atr14": {"atr": 0.0}},
            "unknown",
        )


def _decision() -> SignalDecision:
    return SignalDecision(
        strategy="breakout",
        symbol="XAUUSD",
        timeframe="H1",
        direction="buy",
        confidence=0.9,
        reason="test",
    )


def _bar() -> OHLC:
    return OHLC(
        symbol="XAUUSD",
        timeframe="H1",
        time=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        open=1.0,
        high=1.0,
        low=1.0,
        close=1.0,
        volume=1.0,
    )


class _ProcessDecisionEngine:
    def __init__(
        self,
        *,
        state_edge_overlay: object | None,
        entry_meta_overlay: object | None,
        rejections: list[str],
        blocked_events: list[dict],
    ) -> None:
        self._config = SimpleNamespace(
            enable_state_machine=False,
            confidence=SimpleNamespace(min_confidence=0.1),
        )
        self._circuit_breaker = None
        self._portfolio = SimpleNamespace(_open_positions=[])
        self._strategy_deployments = {}
        self._pending_entry_enabled = False
        self._state_edge_overlay_value = state_edge_overlay
        self._entry_meta_overlay_value = entry_meta_overlay
        self._rejections = rejections
        self._blocked_events = blocked_events

    @property
    def state_edge_overlay(self) -> object | None:
        return self._state_edge_overlay_value

    @property
    def entry_meta_overlay(self) -> object | None:
        return self._entry_meta_overlay_value

    def record_execution_rejection(self, reason: str) -> None:
        self._rejections.append(reason)

    def record_blocked_entry(self, event: dict) -> None:
        self._blocked_events.append(event)


class _StateEdgeOverlayStub:
    def __init__(self, *, allowed: bool) -> None:
        self._allowed = allowed
        self.calls = 0

    def evaluate(
        self,
        bar_time: datetime,
        direction: str,
        *,
        strategy: str = "",
        confidence: float = 0.0,
    ) -> SimpleNamespace:
        del bar_time, direction, strategy, confidence
        self.calls += 1
        return SimpleNamespace(
            allowed=self._allowed,
            reason="allowed" if self._allowed else "state_edge_test_block",
            direction_probability=0.1,
            threshold=0.65,
            prediction=None,
        )


class _EntryMetaOverlayStub:
    def __init__(self, *, allowed: bool) -> None:
        self._allowed = allowed
        self.calls = 0

    def evaluate(
        self,
        bar_time: datetime,
        strategy: str,
        direction: str,
        *,
        confidence: float = 0.0,
        feature_context=None,
    ) -> SimpleNamespace:
        del bar_time, strategy, direction, confidence, feature_context
        self.calls += 1
        return SimpleNamespace(
            allowed=self._allowed,
            reason="allowed" if self._allowed else "entry_meta_test_block",
            take_entry_prob=0.1,
            block_entry_prob=0.9,
            threshold=0.65,
            prediction=None,
        )


class _EntryMetaOverlayCapturingContext:
    def __init__(self, captured: list[object]) -> None:
        self._captured = captured

    def evaluate(
        self,
        bar_time: datetime,
        strategy: str,
        direction: str,
        *,
        confidence: float = 0.0,
        feature_context=None,
    ) -> SimpleNamespace:
        del bar_time, strategy, direction, confidence
        self._captured.append(feature_context)
        return SimpleNamespace(
            allowed=False,
            reason="entry_meta_test_block",
            take_entry_prob=0.9,
            block_entry_prob=0.1,
            threshold=0.65,
            prediction=None,
        )


class _NoEntryMetaOverlayPortEngine:
    def __init__(self) -> None:
        self._config = SimpleNamespace(
            enable_state_machine=False,
            confidence=SimpleNamespace(min_confidence=0.1),
        )
        self._circuit_breaker = None
        self._portfolio = SimpleNamespace(_open_positions=[])
        self._strategy_deployments = {}
        self._pending_entry_enabled = False

    @property
    def state_edge_overlay(self) -> None:
        return None
