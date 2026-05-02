from __future__ import annotations

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
    report = overlay.report()
    assert report["artifact_status"] == "refit"
    assert report["observed"] == 1
    assert report["allowed"] == 1
    assert report["blocked"] == 0
    assert report["missing_predictions"] == 0
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


def test_filter_mode_allows_accepted_artifact_when_probability_meets_threshold() -> None:
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
    assert verdict.reason == "entry_meta_prediction_missing"
    assert verdict.take_entry_prob == 1.0
    assert verdict.block_entry_prob == 0.0
    report = overlay.report()
    assert report["observed"] == 1
    assert report["allowed"] == 1
    assert report["missing_predictions"] == 1


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
    entry_meta_overlay = EntryMetaBacktestOverlay(_artifact(), mode="filter", threshold=0.65)
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
    ) -> SimpleNamespace:
        del bar_time, strategy, direction, confidence
        self.calls += 1
        return SimpleNamespace(
            allowed=self._allowed,
            reason="allowed" if self._allowed else "entry_meta_test_block",
            take_entry_prob=0.1,
            block_entry_prob=0.9,
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
