from __future__ import annotations

from src.signals.models import SignalContext
from src.signals.strategies.structured.breakout_follow import StructuredBreakoutFollow


def _make_context(
    *,
    plus_di: float = 28.0,
    minus_di: float = 12.0,
    adx: float = 26.0,
    adx_d3: float = 2.2,
    rsi: float = 58.0,
    consensus: float = 0.67,
    htf_direction: int = 1,
) -> SignalContext:
    return SignalContext(
        symbol="XAUUSD",
        timeframe="M30",
        strategy="structured_breakout_follow",
        indicators={
            "adx14": {
                "adx": adx,
                "adx_d3": adx_d3,
                "plus_di": plus_di,
                "minus_di": minus_di,
            },
            "rsi14": {"rsi": rsi, "rsi_d3": 1.0},
            "atr14": {"atr": 18.0},
            "bar_stats20": {"body_ratio": 1.4},
            "volume_ratio20": {"volume_ratio": 1.5},
            "momentum_consensus14": {
                "momentum_consensus": consensus,
                "bullish_votes": 3.0,
                "bearish_votes": 0.0,
            },
        },
        metadata={
            "_regime": "breakout",
            "market_structure": {
                "breakout_state": "above_previous_day_high",
                "breached_levels": ["previous_day_high"],
                "first_pullback_state": "none",
            },
        },
        htf_indicators={"H1": {"supertrend14": {"direction": htf_direction}}},
    )


def test_breakout_follow_accepts_consistent_momentum_consensus() -> None:
    strategy = StructuredBreakoutFollow()
    decision = strategy.evaluate(_make_context())

    assert decision.direction == "buy"
    assert decision.confidence > 0.0
    assert "momentum_consensus14" in decision.used_indicators
    assert decision.metadata["promoted_indicators"] == ["momentum_consensus14"]
    assert decision.metadata["research_provenance"] == ["derived.momentum_consensus"]


def test_breakout_follow_rejects_di_breakout_when_momentum_consensus_conflicts() -> None:
    strategy = StructuredBreakoutFollow()
    decision = strategy.evaluate(_make_context(consensus=-0.67))

    assert decision.direction == "hold"
