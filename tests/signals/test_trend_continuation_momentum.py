from __future__ import annotations

from src.signals.models import SignalContext
from src.signals.strategies.structured.trend_continuation import (
    StructuredTrendContinuation,
)


def _make_context(
    *,
    consensus: float = 0.67,
    htf_direction: int = 1,
    close: float = 3052.0,
    htf_ema: float = 3040.0,
    rsi: float = 42.0,
) -> SignalContext:
    return SignalContext(
        symbol="XAUUSD",
        timeframe="H1",
        strategy="structured_trend_h4_momentum",
        indicators={
            "rsi14": {"rsi": rsi, "rsi_d3": -1.0 if htf_direction == 1 else 1.0},
            "atr14": {"atr": 18.0},
            "volume_ratio20": {"volume_ratio": 1.4},
            "momentum_consensus14": {
                "momentum_consensus": consensus,
                "bullish_votes": 3.0,
                "bearish_votes": 0.0,
            },
            "donchian20": {"close": close},
        },
        metadata={
            "_regime": "trending",
            "market_structure": {
                "structure_bias": "bullish_pullback",
                "close_price": close,
            },
        },
        htf_indicators={
            "H4": {
                "supertrend14": {"direction": htf_direction},
                "adx14": {"adx": 28.0},
                "ema50": {"ema": htf_ema},
            }
        },
    )


def _strategy() -> StructuredTrendContinuation:
    return StructuredTrendContinuation(
        name="structured_trend_h4_momentum",
        htf="H4",
        use_momentum_consensus=True,
    )


def test_trend_h4_momentum_accepts_h1_buy_alignment() -> None:
    decision = _strategy().evaluate(_make_context())

    assert decision.direction == "buy"
    assert decision.confidence > 0.0
    assert "momentum_consensus14" in decision.used_indicators
    assert decision.metadata["promoted_indicators"] == ["momentum_consensus14"]
    assert decision.metadata["research_provenance"] == ["derived.momentum_consensus"]
    assert decision.metadata["entry_spec"]["entry_type"] == "limit"


def test_trend_h4_momentum_rejects_negative_consensus() -> None:
    decision = _strategy().evaluate(_make_context(consensus=-0.34))

    assert decision.direction == "hold"
    assert "mom=" in decision.reason


def test_trend_h4_momentum_accepts_sell_side_when_consensus_is_negative() -> None:
    decision = _strategy().evaluate(
        _make_context(
            consensus=-0.67,
            htf_direction=-1,
            close=3028.0,
            htf_ema=3040.0,
            rsi=58.0,
        )
    )

    assert decision.direction == "sell"
    assert decision.metadata["entry_spec"]["entry_type"] == "limit"


def test_trend_h4_momentum_direction_lock_remains_available_for_explicit_long_only_mode() -> None:
    strategy = StructuredTrendContinuation(
        name="structured_trend_h4_momentum_long_only",
        htf="H4",
        use_momentum_consensus=True,
        direction_lock="buy",
    )
    decision = strategy.evaluate(
        _make_context(
            consensus=-0.67,
            htf_direction=-1,
            close=3028.0,
            htf_ema=3040.0,
            rsi=58.0,
        )
    )

    assert decision.direction == "hold"
    assert decision.reason == "direction_locked:buy"
