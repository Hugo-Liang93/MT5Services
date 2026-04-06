from __future__ import annotations

from src.signals.models import SignalContext
from src.signals.strategies.legacy.breakout import (
    BollingerBreakoutStrategy,
    DonchianBreakoutStrategy,
)
from src.signals.strategies.legacy.trend import SmaTrendStrategy


def test_bollinger_breakout_boosts_reclaim_reversion() -> None:
    strategy = BollingerBreakoutStrategy()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M5",
        strategy=strategy.name,
        indicators={"boll20": {"bb_upper": 3010.0, "bb_lower": 2995.0, "bb_mid": 3002.0, "close": 2994.0}},
        metadata={"market_structure": {"reclaim_state": "bullish_reclaim_previous_day_low", "structure_bias": "bullish_reclaim"}},
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "buy"
    assert decision.confidence > 0.55
    assert decision.metadata["reclaim_state"] == "bullish_reclaim_previous_day_low"


def test_donchian_breakout_blocks_failed_structure_breakout() -> None:
    strategy = DonchianBreakoutStrategy()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M5",
        strategy=strategy.name,
        indicators={
            "donchian20": {"donchian_upper": 3000.0, "donchian_lower": 2988.0, "close": 3001.0},
            "adx14": {"adx": 28.0, "plus_di": 30.0, "minus_di": 18.0},
        },
        metadata={"market_structure": {"breakout_state": "above_previous_day_high", "reclaim_state": "bearish_reclaim_previous_day_high"}},
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "hold"
    assert decision.reason == "failed_structure_breakout:bearish_reclaim_previous_day_high"


def test_sma_trend_uses_structure_bias_for_alignment() -> None:
    strategy = SmaTrendStrategy()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M5",
        strategy=strategy.name,
        indicators={"sma20": {"sma": 3002.0}, "ema50": {"ema": 2999.0}},
        metadata={"market_structure": {"structure_bias": "bullish_breakout", "reclaim_state": "none"}},
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "buy"
    assert decision.confidence > 0.19
    assert decision.metadata["structure_bias"] == "bullish_breakout"


def test_sma_trend_boosts_bullish_first_pullback_alignment() -> None:
    strategy = SmaTrendStrategy()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M5",
        strategy=strategy.name,
        indicators={"sma20": {"sma": 3002.0}, "ema50": {"ema": 2999.0}},
        metadata={
            "market_structure": {
                "structure_bias": "bullish_pullback",
                "reclaim_state": "none",
                "first_pullback_state": "bullish_first_pullback_previous_day_high",
            }
        },
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "buy"
    assert decision.confidence > 0.3
    assert (
        decision.metadata["first_pullback_state"]
        == "bullish_first_pullback_previous_day_high"
    )


def test_donchian_breakout_boosts_bullish_first_pullback_continuation() -> None:
    strategy = DonchianBreakoutStrategy()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M5",
        strategy=strategy.name,
        indicators={
            "donchian20": {
                "donchian_upper": 3000.0,
                "donchian_lower": 2988.0,
                "close": 3001.0,
            },
            "adx14": {"adx": 28.0, "plus_di": 30.0, "minus_di": 18.0},
        },
        metadata={
            "market_structure": {
                "breakout_state": "above_previous_day_high",
                "reclaim_state": "none",
                "first_pullback_state": "bullish_first_pullback_previous_day_high",
            }
        },
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "buy"
    assert decision.confidence > 0.91
    assert (
        decision.metadata["first_pullback_state"]
        == "bullish_first_pullback_previous_day_high"
    )


def test_bollinger_breakout_boosts_confirmed_bullish_sweep_reversion() -> None:
    strategy = BollingerBreakoutStrategy()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M5",
        strategy=strategy.name,
        indicators={
            "boll20": {
                "bb_upper": 3010.0,
                "bb_lower": 2995.0,
                "bb_mid": 3002.0,
                "close": 2994.0,
            }
        },
        metadata={
            "market_structure": {
                "sweep_confirmation_state": "bullish_sweep_confirmed_previous_day_low",
                "reclaim_state": "bullish_reclaim_previous_day_low",
                "structure_bias": "bullish_sweep_confirmed",
            }
        },
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "buy"
    assert decision.confidence > 0.7
    assert (
        decision.metadata["sweep_confirmation_state"]
        == "bullish_sweep_confirmed_previous_day_low"
    )


def test_donchian_breakout_blocks_confirmed_bearish_sweep_against_buy() -> None:
    strategy = DonchianBreakoutStrategy()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M5",
        strategy=strategy.name,
        indicators={
            "donchian20": {
                "donchian_upper": 3000.0,
                "donchian_lower": 2988.0,
                "close": 3001.0,
            },
            "adx14": {"adx": 28.0, "plus_di": 30.0, "minus_di": 18.0},
        },
        metadata={
            "market_structure": {
                "breakout_state": "above_previous_day_high",
                "reclaim_state": "none",
                "sweep_confirmation_state": "bearish_sweep_confirmed_previous_day_high",
            }
        },
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "hold"
    assert (
        decision.reason
        == "failed_structure_breakout:bearish_sweep_confirmed_previous_day_high"
    )


def test_sma_trend_penalizes_opposing_sweep_confirmation() -> None:
    strategy = SmaTrendStrategy()
    context = SignalContext(
        symbol="XAUUSD",
        timeframe="M5",
        strategy=strategy.name,
        indicators={"sma20": {"sma": 3002.0}, "ema50": {"ema": 2999.0}},
        metadata={
            "market_structure": {
                "structure_bias": "bearish_sweep_confirmed",
                "reclaim_state": "none",
                "sweep_confirmation_state": "bearish_sweep_confirmed_previous_day_high",
            }
        },
    )

    decision = strategy.evaluate(context)

    assert decision.direction == "buy"
    assert decision.confidence < 0.05
    assert (
        decision.metadata["sweep_confirmation_state"]
        == "bearish_sweep_confirmed_previous_day_high"
    )
