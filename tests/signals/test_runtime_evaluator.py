from __future__ import annotations

from datetime import datetime, timezone

from src.signals.models import SignalDecision
from src.signals.orchestration.runtime_evaluator import apply_confidence_adjustments


class _RuntimeStub:
    def __init__(self) -> None:
        self._strategy_intrabar_decay = {}
        self._intrabar_confidence_factor = 0.85
        self.confidence_floor = 0.10


def _decision(*, confidence: float) -> SignalDecision:
    return SignalDecision(
        strategy="structured_breakout_follow",
        symbol="XAUUSD",
        timeframe="M15",
        direction="buy",
        confidence=confidence,
        reason="test",
        timestamp=datetime(2026, 4, 13, 8, 0, tzinfo=timezone.utc),
        confidence_trace=[("raw", confidence)],
    )


def test_economic_decay_is_not_revived_by_confidence_floor(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.signals.orchestration.runtime_evaluator._compute_economic_event_decay",
        lambda runtime, symbol: 0.0,
    )

    adjusted = apply_confidence_adjustments(
        _RuntimeStub(),
        _decision(confidence=0.42),
        "XAUUSD",
        "M15",
        "structured_breakout_follow",
        "confirmed",
        {},
    )

    assert adjusted.confidence == 0.0
    assert adjusted.confidence_trace[-1] == ("economic_event_decay", 0.0)


def test_confidence_floor_still_applies_without_economic_decay(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.signals.orchestration.runtime_evaluator._compute_economic_event_decay",
        lambda runtime, symbol: 1.0,
    )

    adjusted = apply_confidence_adjustments(
        _RuntimeStub(),
        _decision(confidence=0.04),
        "XAUUSD",
        "M15",
        "structured_breakout_follow",
        "confirmed",
        {},
    )

    assert adjusted.confidence == 0.10
