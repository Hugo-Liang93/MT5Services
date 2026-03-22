"""HTF (Higher Time Frame) bonus/penalty tests for strategies that consume htf_indicators.

Covers 5 strategies x 3 cases = 15 tests:
  - SmaTrendStrategy     (D1 sma20/ema50 alignment)
  - SupertrendStrategy   (H1 supertrend14 direction)
  - MacdMomentumStrategy (D1 macd hist direction)
  - DonchianBreakoutStrategy (H1 donchian20 channel position)
  - BollingerBreakoutStrategy (H1 boll20 squeeze amplification)
"""

from __future__ import annotations

import pytest

from src.signals.models import SignalContext, SignalDecision
from src.signals.strategies.trend import (
    MacdMomentumStrategy,
    SmaTrendStrategy,
    SupertrendStrategy,
)
from src.signals.strategies.breakout import (
    BollingerBreakoutStrategy,
    DonchianBreakoutStrategy,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_context(
    indicators: dict,
    htf_indicators: dict | None = None,
    metadata: dict | None = None,
    symbol: str = "XAUUSD",
    timeframe: str = "M5",
    strategy: str = "",
) -> SignalContext:
    return SignalContext(
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy,
        indicators=indicators,
        htf_indicators=htf_indicators or {},
        metadata=metadata or {},
    )


# ===========================================================================
# SmaTrendStrategy — D1 sma20/ema50 alignment
# ===========================================================================

class TestSmaTrendHTF:
    """SmaTrendStrategy reads D1 sma20/ema50 for HTF trend confirmation.

    - D1 bullish (sma > ema) + buy signal  => +0.10 bonus
    - D1 bearish (sma < ema) + buy signal  => -0.08 penalty
    - D1 absent                             => no change
    """

    @staticmethod
    def _buy_indicators() -> dict:
        """Indicators that produce a buy: sma20 > ema50 with enough spread."""
        return {
            "sma20": {"sma": 2020.0},
            "ema50": {"ema": 2000.0},
        }

    def test_sma_trend_htf_aligned_bonus(self) -> None:
        ctx = _make_context(
            indicators=self._buy_indicators(),
            htf_indicators={
                "D1": {
                    "sma20": {"sma": 2100.0},  # D1 bullish: sma > ema
                    "ema50": {"ema": 2050.0},
                },
            },
        )
        decision = SmaTrendStrategy().evaluate(ctx)
        assert decision.action == "buy"
        assert decision.metadata["htf_bonus"] == 0.10

    def test_sma_trend_htf_conflict_penalty(self) -> None:
        ctx = _make_context(
            indicators=self._buy_indicators(),
            htf_indicators={
                "D1": {
                    "sma20": {"sma": 2000.0},  # D1 bearish: sma < ema
                    "ema50": {"ema": 2100.0},
                },
            },
        )
        decision = SmaTrendStrategy().evaluate(ctx)
        assert decision.action == "buy"
        assert decision.metadata["htf_bonus"] == -0.08

    def test_sma_trend_htf_absent_no_effect(self) -> None:
        ctx_no_htf = _make_context(indicators=self._buy_indicators())
        ctx_empty = _make_context(
            indicators=self._buy_indicators(),
            htf_indicators={"D1": {}},
        )
        d_no = SmaTrendStrategy().evaluate(ctx_no_htf)
        d_em = SmaTrendStrategy().evaluate(ctx_empty)
        assert d_no.metadata["htf_bonus"] == 0.0
        assert d_em.metadata["htf_bonus"] == 0.0
        # Confidence should be identical when HTF is absent
        assert d_no.confidence == pytest.approx(d_em.confidence)


# ===========================================================================
# SupertrendStrategy — H1 supertrend14 direction
# ===========================================================================

class TestSupertrendHTF:
    """SupertrendStrategy reads H1 supertrend14.direction.

    - H1 direction matches (buy + dir=1)   => +0.08 bonus
    - H1 direction conflicts (buy + dir=-1) => -0.05 penalty
    - H1 absent                              => no change
    """

    @staticmethod
    def _buy_indicators() -> dict:
        """Supertrend buy: direction=1 with ADX above threshold."""
        return {
            "supertrend14": {"direction": 1.0, "supertrend": 2950.0},
            "adx14": {"adx": 30.0},
        }

    def test_supertrend_htf_aligned_bonus(self) -> None:
        ctx = _make_context(
            indicators=self._buy_indicators(),
            htf_indicators={
                "H1": {
                    "supertrend14": {"direction": 1.0},  # same direction
                    "adx14": {"adx": 28.0},
                },
            },
        )
        decision = SupertrendStrategy().evaluate(ctx)
        assert decision.action == "buy"
        assert decision.metadata["htf_bonus"] == 0.08

    def test_supertrend_htf_conflict_penalty(self) -> None:
        ctx = _make_context(
            indicators=self._buy_indicators(),
            htf_indicators={
                "H1": {
                    "supertrend14": {"direction": -1.0},  # opposite direction
                    "adx14": {"adx": 28.0},
                },
            },
        )
        decision = SupertrendStrategy().evaluate(ctx)
        assert decision.action == "buy"
        assert decision.metadata["htf_bonus"] == -0.05

    def test_supertrend_htf_absent_no_effect(self) -> None:
        ctx_no = _make_context(indicators=self._buy_indicators())
        ctx_empty = _make_context(
            indicators=self._buy_indicators(),
            htf_indicators={"H1": {"supertrend14": {}}},  # no 'direction' key
        )
        d_no = SupertrendStrategy().evaluate(ctx_no)
        d_em = SupertrendStrategy().evaluate(ctx_empty)
        assert d_no.metadata["htf_bonus"] == 0.0
        assert d_em.metadata["htf_bonus"] == 0.0
        assert d_no.confidence == pytest.approx(d_em.confidence)


# ===========================================================================
# MacdMomentumStrategy — D1 MACD hist direction
# ===========================================================================

class TestMacdMomentumHTF:
    """MacdMomentumStrategy reads D1 macd.hist.

    - D1 hist > 0 + buy signal  => +0.08 bonus
    - D1 hist < 0 + buy signal  => -0.06 penalty
    - D1 absent                  => no change
    """

    @staticmethod
    def _buy_indicators() -> dict:
        """MACD buy: hist > 0 and macd > signal."""
        return {
            "macd": {
                "macd": 2.5,
                "signal": 1.0,
                "hist": 1.5,
            },
        }

    def test_macd_momentum_htf_aligned_bonus(self) -> None:
        ctx = _make_context(
            indicators=self._buy_indicators(),
            htf_indicators={
                "D1": {
                    "macd": {"hist": 3.0},  # D1 hist positive => aligned with buy
                },
            },
        )
        decision = MacdMomentumStrategy().evaluate(ctx)
        assert decision.action == "buy"
        assert decision.metadata["htf_bonus"] == 0.08

    def test_macd_momentum_htf_conflict_penalty(self) -> None:
        ctx = _make_context(
            indicators=self._buy_indicators(),
            htf_indicators={
                "D1": {
                    "macd": {"hist": -2.0},  # D1 hist negative => conflicts with buy
                },
            },
        )
        decision = MacdMomentumStrategy().evaluate(ctx)
        assert decision.action == "buy"
        assert decision.metadata["htf_bonus"] == -0.06

    def test_macd_momentum_htf_absent_no_effect(self) -> None:
        ctx_no = _make_context(indicators=self._buy_indicators())
        ctx_empty = _make_context(
            indicators=self._buy_indicators(),
            htf_indicators={"D1": {"macd": {}}},  # no 'hist' key
        )
        d_no = MacdMomentumStrategy().evaluate(ctx_no)
        d_em = MacdMomentumStrategy().evaluate(ctx_empty)
        assert d_no.metadata["htf_bonus"] == 0.0
        assert d_em.metadata["htf_bonus"] == 0.0
        assert d_no.confidence == pytest.approx(d_em.confidence)


# ===========================================================================
# DonchianBreakoutStrategy — H1 donchian20 channel position
# ===========================================================================

class TestDonchianBreakoutHTF:
    """DonchianBreakoutStrategy reads H1 donchian20 upper/lower.

    - H1 buy breakout (close >= h1_upper)  => +0.10 bonus
    - H1 conflict (buy but close < h1_lower) => -0.05 penalty
    - H1 absent                               => no change
    """

    @staticmethod
    def _buy_indicators(close: float = 2050.0) -> dict:
        """Donchian buy: close at upper channel, ADX above threshold."""
        return {
            "donchian20": {
                "donchian_upper": 2050.0,
                "donchian_lower": 1950.0,
                "close": close,
            },
            "adx14": {"adx": 30.0, "plus_di": 25.0, "minus_di": 15.0},
        }

    def test_donchian_breakout_htf_aligned_bonus(self) -> None:
        ctx = _make_context(
            indicators=self._buy_indicators(close=2050.0),
            htf_indicators={
                "H1": {
                    "donchian20": {
                        "donchian_upper": 2040.0,  # close >= h1_upper => strong breakout
                        "donchian_lower": 1940.0,
                    },
                    "adx14": {"adx": 30.0},
                },
            },
        )
        decision = DonchianBreakoutStrategy().evaluate(ctx)
        assert decision.action == "buy"
        assert decision.metadata["htf_bonus"] == 0.10

    def test_donchian_breakout_htf_conflict_penalty(self) -> None:
        ctx = _make_context(
            indicators=self._buy_indicators(close=2050.0),
            htf_indicators={
                "H1": {
                    "donchian20": {
                        "donchian_upper": 2200.0,  # close < h1_lower => H1 bearish
                        "donchian_lower": 2100.0,
                    },
                    "adx14": {"adx": 30.0},
                },
            },
        )
        decision = DonchianBreakoutStrategy().evaluate(ctx)
        assert decision.action == "buy"
        assert decision.metadata["htf_bonus"] == -0.05

    def test_donchian_breakout_htf_absent_no_effect(self) -> None:
        ctx_no = _make_context(indicators=self._buy_indicators())
        ctx_empty = _make_context(
            indicators=self._buy_indicators(),
            htf_indicators={"H1": {"donchian20": {}}},  # missing keys
        )
        d_no = DonchianBreakoutStrategy().evaluate(ctx_no)
        d_em = DonchianBreakoutStrategy().evaluate(ctx_empty)
        assert d_no.metadata["htf_bonus"] == 0.0
        assert d_em.metadata["htf_bonus"] == 0.0
        assert d_no.confidence == pytest.approx(d_em.confidence)


# ===========================================================================
# BollingerBreakoutStrategy — H1 boll20 squeeze amplification
# ===========================================================================

class TestBollingerBreakoutHTF:
    """BollingerBreakoutStrategy reads H1 boll20 bandwidth.

    - H1 BB squeezed (bandwidth < 0.01) => +0.06 bonus (amplifies breakout)
    - H1 BB wide (bandwidth >= 0.01)    => no bonus (htf_bonus == 0)
    - H1 absent                          => no change
    """

    @staticmethod
    def _buy_indicators() -> dict:
        """BB buy: close <= lower band."""
        return {
            "boll20": {
                "bb_upper": 2050.0,
                "bb_lower": 1950.0,
                "bb_mid": 2000.0,
                "close": 1945.0,  # below lower => buy
            },
        }

    def test_bollinger_breakout_htf_aligned_bonus(self) -> None:
        # H1 BB also squeezed (bandwidth < 0.01) => amplifies breakout signal
        h1_mid = 2000.0
        h1_bw = 0.008  # < 0.01 threshold
        h1_half = h1_mid * h1_bw / 2.0
        ctx = _make_context(
            indicators=self._buy_indicators(),
            htf_indicators={
                "H1": {
                    "boll20": {
                        "bb_upper": h1_mid + h1_half,
                        "bb_lower": h1_mid - h1_half,
                        "bb_mid": h1_mid,
                    },
                },
            },
        )
        decision = BollingerBreakoutStrategy().evaluate(ctx)
        assert decision.action == "buy"
        assert decision.metadata["htf_bonus"] == 0.06

    def test_bollinger_breakout_htf_conflict_penalty(self) -> None:
        # H1 BB not squeezed (bandwidth >= 0.01) => no bonus, htf_bonus = 0
        # Note: Bollinger HTF logic only boosts on squeeze; wide BB means no bonus (not penalty).
        h1_mid = 2000.0
        h1_bw = 0.05  # well above 0.01
        h1_half = h1_mid * h1_bw / 2.0
        ctx = _make_context(
            indicators=self._buy_indicators(),
            htf_indicators={
                "H1": {
                    "boll20": {
                        "bb_upper": h1_mid + h1_half,
                        "bb_lower": h1_mid - h1_half,
                        "bb_mid": h1_mid,
                    },
                },
            },
        )
        decision = BollingerBreakoutStrategy().evaluate(ctx)
        assert decision.action == "buy"
        # Wide H1 BB means no HTF squeeze bonus
        assert decision.metadata["htf_bonus"] == 0.0

    def test_bollinger_breakout_htf_absent_no_effect(self) -> None:
        ctx_no = _make_context(indicators=self._buy_indicators())
        ctx_empty = _make_context(
            indicators=self._buy_indicators(),
            htf_indicators={"H1": {}},
        )
        d_no = BollingerBreakoutStrategy().evaluate(ctx_no)
        d_em = BollingerBreakoutStrategy().evaluate(ctx_empty)
        assert d_no.metadata["htf_bonus"] == 0.0
        assert d_em.metadata["htf_bonus"] == 0.0
        assert d_no.confidence == pytest.approx(d_em.confidence)
