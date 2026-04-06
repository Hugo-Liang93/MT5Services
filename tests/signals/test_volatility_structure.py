"""Tests for volatility_structure strategies.

Covers: AtrRegimeShift, SwingStructureBreak, RangeMeanReversion,
        BarMomentumSurge per-TF params.
"""

from __future__ import annotations

import pytest

from src.signals.models import SignalContext
from src.signals.strategies.legacy.volatility_structure import (
    AtrRegimeShift,
    BarMomentumSurge,
    RangeBoxBreakout,
    RangeMeanReversion,
    SwingStructureBreak,
)

# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _ctx(strategy_name: str, indicators: dict, tf: str = "H1") -> SignalContext:
    return SignalContext(
        symbol="XAUUSD",
        timeframe=tf,
        strategy=strategy_name,
        indicators=indicators,
    )


# ---------------------------------------------------------------------------
#  RangeBoxBreakout (existing – regression guard)
# ---------------------------------------------------------------------------


class TestRangeBoxBreakout:
    def test_buy_on_box_breakout(self) -> None:
        s = RangeBoxBreakout()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "donchian20": {"donchian_upper": 3010, "donchian_lower": 3000},
                    "atr14": {"atr": 5.0},  # don_width=10 < atr*4=20 → 箱体
                    "adx14": {"adx": 20.0},
                    "boll20": {"bb_mid": 3006.5},  # offset > 0 → buy
                },
            )
        )
        assert decision.direction == "buy"
        assert decision.confidence >= 0.50

    def test_hold_when_no_range(self) -> None:
        s = RangeBoxBreakout()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "donchian20": {"donchian_upper": 3050, "donchian_lower": 3000},
                    "atr14": {"atr": 5.0},  # width=50 > atr*4=20 → 不是箱体
                    "adx14": {"adx": 20.0},
                    "boll20": {"bb_mid": 3025},
                },
            )
        )
        assert decision.direction == "hold"


# ---------------------------------------------------------------------------
#  BarMomentumSurge (per-TF params)
# ---------------------------------------------------------------------------


class TestBarMomentumSurge:
    def test_buy_on_bullish_surge(self) -> None:
        s = BarMomentumSurge()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "bar_stats20": {
                        "body_ratio": 2.5,
                        "close_position": 0.85,
                        "is_bullish": True,
                    },
                    "rsi14": {"rsi": 55.0},
                },
            )
        )
        assert decision.direction == "buy"
        assert decision.confidence >= 0.50

    def test_sell_on_bearish_surge(self) -> None:
        s = BarMomentumSurge()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "bar_stats20": {
                        "body_ratio": 2.5,
                        "close_position": 0.15,
                        "is_bullish": False,
                    },
                    "rsi14": {"rsi": 45.0},
                },
            )
        )
        assert decision.direction == "sell"
        assert decision.confidence >= 0.50

    def test_hold_on_small_bar(self) -> None:
        s = BarMomentumSurge()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "bar_stats20": {
                        "body_ratio": 1.2,
                        "close_position": 0.85,
                        "is_bullish": True,
                    },
                    "rsi14": {"rsi": 50.0},
                },
            )
        )
        assert decision.direction == "hold"

    def test_hold_on_rsi_overbought(self) -> None:
        s = BarMomentumSurge()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "bar_stats20": {
                        "body_ratio": 2.5,
                        "close_position": 0.85,
                        "is_bullish": True,
                    },
                    "rsi14": {"rsi": 82.0},  # > 78 overbought
                },
            )
        )
        assert decision.direction == "hold"


# ---------------------------------------------------------------------------
#  AtrRegimeShift
# ---------------------------------------------------------------------------


class TestAtrRegimeShift:
    def test_buy_on_regime_shift(self) -> None:
        s = AtrRegimeShift()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "adx14": {
                        "adx": 20.0,
                        "plus_di": 25.0,
                        "minus_di": 15.0,
                        "adx_d3": 4.0,
                    },
                    "boll20": {"bb_upper": 3005, "bb_lower": 2995, "bb_mid": 3000},
                    "atr14": {"atr": 10.0},
                },
            )
        )
        # bb_width_pct = 10/3000 = 0.0033 < 0.012 → compression
        # adx=20 in [14, 25] → window
        # adx_d3=4 > 2 → rising
        # di_spread=10 > 5 → buy direction
        assert decision.direction == "buy"
        assert decision.confidence >= 0.50

    def test_sell_on_minus_di_dominant(self) -> None:
        s = AtrRegimeShift()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "adx14": {
                        "adx": 18.0,
                        "plus_di": 12.0,
                        "minus_di": 22.0,
                        "adx_d3": 3.0,
                    },
                    "boll20": {"bb_upper": 3004, "bb_lower": 2996, "bb_mid": 3000},
                    "atr14": {"atr": 10.0},
                },
            )
        )
        assert decision.direction == "sell"

    def test_hold_when_adx_too_high(self) -> None:
        s = AtrRegimeShift()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "adx14": {
                        "adx": 30.0,
                        "plus_di": 25.0,
                        "minus_di": 15.0,
                        "adx_d3": 4.0,
                    },
                    "boll20": {"bb_upper": 3005, "bb_lower": 2995, "bb_mid": 3000},
                    "atr14": {"atr": 10.0},
                },
            )
        )
        assert decision.direction == "hold"

    def test_hold_when_no_compression(self) -> None:
        s = AtrRegimeShift()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "adx14": {
                        "adx": 20.0,
                        "plus_di": 25.0,
                        "minus_di": 15.0,
                        "adx_d3": 4.0,
                    },
                    "boll20": {"bb_upper": 3050, "bb_lower": 2950, "bb_mid": 3000},
                    "atr14": {"atr": 10.0},
                },
            )
        )
        # bb_width_pct = 100/3000 = 0.033 > 0.012 → no compression
        assert decision.direction == "hold"

    def test_hold_when_adx_not_rising(self) -> None:
        s = AtrRegimeShift()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "adx14": {
                        "adx": 20.0,
                        "plus_di": 25.0,
                        "minus_di": 15.0,
                        "adx_d3": 0.5,
                    },
                    "boll20": {"bb_upper": 3005, "bb_lower": 2995, "bb_mid": 3000},
                    "atr14": {"atr": 10.0},
                },
            )
        )
        assert decision.direction == "hold"

    def test_hold_when_no_direction(self) -> None:
        s = AtrRegimeShift()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "adx14": {
                        "adx": 20.0,
                        "plus_di": 18.0,
                        "minus_di": 17.0,
                        "adx_d3": 4.0,
                    },
                    "boll20": {"bb_upper": 3005, "bb_lower": 2995, "bb_mid": 3000},
                    "atr14": {"atr": 10.0},
                },
            )
        )
        # di_spread = 1.0 < 5.0 → no direction
        assert decision.direction == "hold"

    def test_missing_indicator(self) -> None:
        s = AtrRegimeShift()
        decision = s.evaluate(_ctx(s.name, {"adx14": {}}))
        assert decision.direction == "hold"


# ---------------------------------------------------------------------------
#  SwingStructureBreak
# ---------------------------------------------------------------------------


class TestSwingStructureBreak:
    def test_buy_on_upside_breakout(self) -> None:
        s = SwingStructureBreak()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "donchian20": {
                        "donchian_upper": 3000,
                        "donchian_lower": 2970,
                        "close": 3010,
                    },
                    "atr14": {"atr": 10.0},
                    "rsi14": {"rsi": 60.0},
                    "adx14": {"adx": 22.0},
                },
            )
        )
        # up_break = (3010-3000)/3000 = 0.0033 > 0.002 → buy
        assert decision.direction == "buy"
        assert decision.confidence >= 0.50

    def test_sell_on_downside_breakout(self) -> None:
        s = SwingStructureBreak()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "donchian20": {
                        "donchian_upper": 3000,
                        "donchian_lower": 2970,
                        "close": 2960,
                    },
                    "atr14": {"atr": 10.0},
                    "rsi14": {"rsi": 40.0},
                    "adx14": {"adx": 22.0},
                },
            )
        )
        # dn_break = (2970-2960)/2970 = 0.0034 > 0.002 → sell
        assert decision.direction == "sell"
        assert decision.confidence >= 0.50

    def test_hold_when_no_breakout(self) -> None:
        s = SwingStructureBreak()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "donchian20": {
                        "donchian_upper": 3000,
                        "donchian_lower": 2970,
                        "close": 2990,
                    },
                    "atr14": {"atr": 10.0},
                    "rsi14": {"rsi": 50.0},
                    "adx14": {"adx": 22.0},
                },
            )
        )
        assert decision.direction == "hold"

    def test_hold_when_adx_low(self) -> None:
        s = SwingStructureBreak()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "donchian20": {
                        "donchian_upper": 3000,
                        "donchian_lower": 2970,
                        "close": 3010,
                    },
                    "atr14": {"atr": 10.0},
                    "rsi14": {"rsi": 60.0},
                    "adx14": {"adx": 12.0},  # < 18 → too low
                },
            )
        )
        assert decision.direction == "hold"

    def test_hold_when_rsi_overbought(self) -> None:
        s = SwingStructureBreak()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "donchian20": {
                        "donchian_upper": 3000,
                        "donchian_lower": 2970,
                        "close": 3010,
                    },
                    "atr14": {"atr": 10.0},
                    "rsi14": {"rsi": 75.0},  # > 70 → overbought
                    "adx14": {"adx": 22.0},
                },
            )
        )
        assert decision.direction == "hold"

    def test_higher_confidence_with_strong_adx(self) -> None:
        s = SwingStructureBreak()
        weak = s.evaluate(
            _ctx(
                s.name,
                {
                    "donchian20": {
                        "donchian_upper": 3000,
                        "donchian_lower": 2970,
                        "close": 3010,
                    },
                    "atr14": {"atr": 10.0},
                    "rsi14": {"rsi": 55.0},
                    "adx14": {"adx": 20.0},
                },
            )
        )
        strong = s.evaluate(
            _ctx(
                s.name,
                {
                    "donchian20": {
                        "donchian_upper": 3000,
                        "donchian_lower": 2970,
                        "close": 3010,
                    },
                    "atr14": {"atr": 10.0},
                    "rsi14": {"rsi": 55.0},
                    "adx14": {"adx": 35.0},
                },
            )
        )
        assert strong.confidence > weak.confidence


# ---------------------------------------------------------------------------
#  RangeMeanReversion
# ---------------------------------------------------------------------------


class TestRangeMeanReversion:
    def test_buy_at_lower_bb(self) -> None:
        s = RangeMeanReversion()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "boll20": {
                        "bb_upper": 3020,
                        "bb_lower": 2980,
                        "bb_mid": 3000,
                        "close": 2982,
                    },
                    "atr14": {"atr": 10.0},
                    "adx14": {"adx": 15.0},
                    "rsi14": {"rsi": 22.0},
                },
            )
        )
        # bb_width=40 > atr*1.5=15 → ok
        # bb_position = (2982-2980)/40 = 0.05 < 0.10 → buy
        assert decision.direction == "buy"
        assert decision.confidence >= 0.50

    def test_sell_at_upper_bb(self) -> None:
        s = RangeMeanReversion()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "boll20": {
                        "bb_upper": 3020,
                        "bb_lower": 2980,
                        "bb_mid": 3000,
                        "close": 3018,
                    },
                    "atr14": {"atr": 10.0},
                    "adx14": {"adx": 15.0},
                    "rsi14": {"rsi": 78.0},
                },
            )
        )
        # bb_position = (3018-2980)/40 = 0.95 > 0.90 → sell
        assert decision.direction == "sell"
        assert decision.confidence >= 0.50

    def test_hold_in_mid_zone(self) -> None:
        s = RangeMeanReversion()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "boll20": {
                        "bb_upper": 3020,
                        "bb_lower": 2980,
                        "bb_mid": 3000,
                        "close": 3000,
                    },
                    "atr14": {"atr": 10.0},
                    "adx14": {"adx": 15.0},
                    "rsi14": {"rsi": 50.0},
                },
            )
        )
        assert decision.direction == "hold"

    def test_hold_when_trending(self) -> None:
        s = RangeMeanReversion()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "boll20": {
                        "bb_upper": 3020,
                        "bb_lower": 2980,
                        "bb_mid": 3000,
                        "close": 2982,
                    },
                    "atr14": {"atr": 10.0},
                    "adx14": {"adx": 28.0},  # > 22 → trending
                    "rsi14": {"rsi": 25.0},
                },
            )
        )
        assert decision.direction == "hold"

    def test_hold_when_bb_too_narrow(self) -> None:
        s = RangeMeanReversion()
        decision = s.evaluate(
            _ctx(
                s.name,
                {
                    "boll20": {
                        "bb_upper": 3005,
                        "bb_lower": 2995,
                        "bb_mid": 3000,
                        "close": 2996,
                    },
                    "atr14": {"atr": 10.0},  # bb_width=10 < atr*1.5=15 → too narrow
                    "adx14": {"adx": 15.0},
                    "rsi14": {"rsi": 25.0},
                },
            )
        )
        assert decision.direction == "hold"

    def test_rsi_bonus_increases_confidence(self) -> None:
        s = RangeMeanReversion()
        no_rsi_extreme = s.evaluate(
            _ctx(
                s.name,
                {
                    "boll20": {
                        "bb_upper": 3020,
                        "bb_lower": 2980,
                        "bb_mid": 3000,
                        "close": 2982,
                    },
                    "atr14": {"atr": 10.0},
                    "adx14": {"adx": 15.0},
                    "rsi14": {"rsi": 40.0},  # not extreme
                },
            )
        )
        with_rsi_extreme = s.evaluate(
            _ctx(
                s.name,
                {
                    "boll20": {
                        "bb_upper": 3020,
                        "bb_lower": 2980,
                        "bb_mid": 3000,
                        "close": 2982,
                    },
                    "atr14": {"atr": 10.0},
                    "adx14": {"adx": 15.0},
                    "rsi14": {"rsi": 18.0},  # extreme low → bonus
                },
            )
        )
        assert with_rsi_extreme.confidence > no_rsi_extreme.confidence
