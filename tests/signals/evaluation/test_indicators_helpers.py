"""Regression tests for src/signals/evaluation/indicators_helpers.py.

P3 历史 bug: hold_decision() 用 SignalDecision(action="hold", ...)，但
SignalDecision 真实字段是 direction 不是 action。直接调用即抛 TypeError，
因此该 helper 长期处于"声明 public API 但坏"状态。
"""

from __future__ import annotations

from src.signals.evaluation.indicators_helpers import hold_decision
from src.signals.models import SignalDecision


def test_hold_decision_does_not_raise_typeerror() -> None:
    """回归：旧实现传 action= kwarg → TypeError: unexpected keyword argument 'action'。"""
    decision = hold_decision(
        strategy="my_strategy",
        symbol="XAUUSD",
        timeframe="H1",
    )
    assert isinstance(decision, SignalDecision)


def test_hold_decision_returns_hold_direction() -> None:
    """contract: hold_decision 应产生 direction='hold' 的 SignalDecision。"""
    decision = hold_decision(
        strategy="trend_continuation",
        symbol="EURUSD",
        timeframe="M15",
    )
    assert decision.direction == "hold"
    assert decision.confidence == 0.0
    assert decision.strategy == "trend_continuation"
    assert decision.symbol == "EURUSD"
    assert decision.timeframe == "M15"


def test_hold_decision_default_reason() -> None:
    decision = hold_decision(strategy="s", symbol="XAUUSD", timeframe="H1")
    assert decision.reason == "insufficient_indicators"


def test_hold_decision_custom_reason() -> None:
    decision = hold_decision(
        strategy="s",
        symbol="XAUUSD",
        timeframe="H1",
        reason="atr_unavailable",
    )
    assert decision.reason == "atr_unavailable"
