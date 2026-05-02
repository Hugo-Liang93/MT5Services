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
    overlay = EntryMetaBacktestOverlay(_artifact(status="refit"), mode="shadow", threshold=0.65)

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
    engine = SimpleNamespace(
        _config=SimpleNamespace(
            enable_state_machine=False,
            confidence=SimpleNamespace(min_confidence=0.1),
        ),
        _circuit_breaker=None,
        _portfolio=SimpleNamespace(_open_positions=[]),
        _strategy_deployments={},
        _state_edge_overlay=None,
        _entry_meta_overlay=overlay,
        _pending_entry_enabled=False,
        record_execution_rejection=lambda reason: rejections.append(reason),
        record_blocked_entry=lambda event: blocked_events.append(event),
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

    assert engine.entry_meta_overlay_report() == overlay.report()
