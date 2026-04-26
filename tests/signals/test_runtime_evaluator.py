from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.signals.models import SignalDecision
from src.signals.orchestration.runtime_evaluator import apply_confidence_adjustments


class _StubDecayService:
    def __init__(self, decay: float) -> None:
        self._decay = decay
        self.calls: list[tuple[str, datetime]] = []

    def decay_factor(self, symbol: str, *, at_time: datetime) -> float:
        self.calls.append((symbol, at_time))
        return self._decay


class _RuntimeStub:
    def __init__(self, *, decay: float) -> None:
        self._strategy_intrabar_decay: dict[str, float] = {}
        self._intrabar_confidence_factor = 0.85
        self.confidence_floor = 0.10
        self._economic_decay_service: Any = _StubDecayService(decay)

    @property
    def economic_decay_service(self) -> Any:
        return self._economic_decay_service


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


_EVENT_TIME = datetime(2026, 4, 13, 8, 0, tzinfo=timezone.utc)


def test_economic_decay_is_not_revived_by_confidence_floor() -> None:
    runtime = _RuntimeStub(decay=0.0)
    adjusted = apply_confidence_adjustments(
        runtime,
        _decision(confidence=0.42),
        "XAUUSD",
        "M15",
        "structured_breakout_follow",
        "confirmed",
        {},
        _EVENT_TIME,
    )
    assert adjusted.confidence == 0.0
    assert adjusted.confidence_trace[-1] == ("economic_event_decay", 0.0)


def test_confidence_floor_still_applies_without_economic_decay() -> None:
    runtime = _RuntimeStub(decay=1.0)
    adjusted = apply_confidence_adjustments(
        runtime,
        _decision(confidence=0.04),
        "XAUUSD",
        "M15",
        "structured_breakout_follow",
        "confirmed",
        {},
        _EVENT_TIME,
    )
    assert adjusted.confidence == 0.10


def test_decay_service_called_with_event_time_not_wall_clock() -> None:
    """关键回归：apply_confidence_adjustments 必须把 event_time 透传给 service。"""
    runtime = _RuntimeStub(decay=0.5)
    apply_confidence_adjustments(
        runtime,
        _decision(confidence=0.40),
        "XAUUSD",
        "M15",
        "structured_breakout_follow",
        "confirmed",
        {},
        _EVENT_TIME,
    )
    service = runtime.economic_decay_service
    assert service.calls == [("XAUUSD", _EVENT_TIME)]


def test_no_decay_when_service_missing() -> None:
    runtime = _RuntimeStub(decay=0.0)
    runtime._economic_decay_service = None
    adjusted = apply_confidence_adjustments(
        runtime,
        _decision(confidence=0.42),
        "XAUUSD",
        "M15",
        "structured_breakout_follow",
        "confirmed",
        {},
        _EVENT_TIME,
    )
    # service 缺失 → decay 等价 1.0 → confidence 不变
    assert adjusted.confidence == 0.42
    assert all(t[0] != "economic_event_decay" for t in adjusted.confidence_trace)
