"""StructuredStrongTrendFollow 单元测试。

挖掘来源：2026-04-17 H1 rule_mining #5
  IF adx14.adx > 40.12 AND macd_fast.hist <= 1.61 AND roc12.roc > -1.17 THEN buy
"""

from __future__ import annotations

import pytest

from src.signals.evaluation.regime import RegimeType
from src.signals.models import SignalContext
from src.signals.strategies.structured.base import (
    ExitMode,
    HtfPolicy,
)
from src.signals.strategies.structured.strong_trend_follow import (
    StructuredStrongTrendFollow,
)


def _make_context(
    *,
    adx: float = 50.0,
    adx_d3: float = 1.0,
    plus_di: float = 35.0,
    minus_di: float = 15.0,
    macd_hist: float = 0.5,
    roc: float = 0.5,
    atr: float = 20.0,
    volume_ratio: float = 1.0,
    regime: str = "trending",
    compression_state: str = "none",
    breakout_state: str = "none",
) -> SignalContext:
    """构造测试用 SignalContext。默认所有条件满足 → buy。"""
    return SignalContext(
        symbol="XAUUSD",
        timeframe="H1",
        strategy="structured_strong_trend_follow",
        indicators={
            "adx14": {
                "adx": adx,
                "adx_d3": adx_d3,
                "plus_di": plus_di,
                "minus_di": minus_di,
            },
            "macd_fast": {"macd": 1.0, "signal": 0.5, "hist": macd_hist},
            "roc12": {"roc": roc},
            "atr14": {"atr": atr},
            "volume_ratio20": {"volume_ratio": volume_ratio},
        },
        metadata={
            "_regime": regime,
            "market_structure": {
                "compression_state": compression_state,
                "breakout_state": breakout_state,
            },
        },
        htf_indicators={},
    )


class TestClassAttributes:
    """类元数据验证。"""

    def setup_method(self) -> None:
        self.strategy = StructuredStrongTrendFollow()

    def test_name(self) -> None:
        assert self.strategy.name == "structured_strong_trend_follow"

    def test_category(self) -> None:
        assert self.strategy.category == "trend_continuation"

    def test_htf_policy_none(self) -> None:
        assert self.strategy.htf_policy == HtfPolicy.NONE

    def test_preferred_scopes_confirmed_only(self) -> None:
        assert self.strategy.preferred_scopes == ("confirmed",)

    def test_required_indicators(self) -> None:
        assert set(self.strategy.required_indicators) == {
            "atr14",
            "adx14",
            "macd_fast",
            "roc12",
            "volume_ratio20",
        }

    def test_regime_affinity_trending_primary(self) -> None:
        assert self.strategy.regime_affinity[RegimeType.TRENDING] == 1.00
        assert self.strategy.regime_affinity[RegimeType.BREAKOUT] == 0.60
        assert self.strategy.regime_affinity[RegimeType.RANGING] == 0.00
        assert self.strategy.regime_affinity[RegimeType.UNCERTAIN] == 0.20


class TestWhyGate:
    """_why() 硬门控测试。"""

    def setup_method(self) -> None:
        self.strategy = StructuredStrongTrendFollow()

    def test_adx_below_threshold_rejected(self) -> None:
        """ADX=30 < 40 → hold。"""
        ctx = _make_context(adx=30.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"
        assert "adx_low" in decision.reason

    def test_adx_at_threshold_rejected(self) -> None:
        """ADX=40 (=threshold) → hold（需严格 >）。"""
        ctx = _make_context(adx=40.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_di_not_bullish_rejected(self) -> None:
        """plus_di=20 <= minus_di=30 → hold（空头结构）。"""
        ctx = _make_context(plus_di=20.0, minus_di=30.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"
        assert "di_not_bullish" in decision.reason

    def test_di_equal_rejected(self) -> None:
        """plus_di = minus_di → hold（需严格 >）。"""
        ctx = _make_context(plus_di=25.0, minus_di=25.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_adx_d3_zero_rejected_mutex_with_exhaustion(self) -> None:
        """adx_d3=0 → hold（临界点归 regime_exhaustion）。"""
        ctx = _make_context(adx_d3=0.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"
        assert "adx_not_rising" in decision.reason

    def test_adx_d3_negative_rejected(self) -> None:
        """adx_d3=-1 → hold（趋势在减弱，让给 regime_exhaustion）。"""
        ctx = _make_context(adx_d3=-1.0)
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"

    def test_no_adx_data_rejected(self) -> None:
        """缺失 ADX 数据 → hold。"""
        ctx = _make_context()
        ctx.indicators["adx14"] = {}
        decision = self.strategy.evaluate(ctx)
        assert decision.direction == "hold"
        assert "no_adx_data" in decision.reason

    def test_why_passes_all_bullish_strong_trend(self) -> None:
        """ADX=50 + plus_di>minus_di + adx_d3=1 + MACD/ROC pending → 先不 hold 于 why。

        但因 _when/_entry/_exit 还未实现，evaluate 会抛 NotImplementedError。
        仅验证 _why 本身逻辑：直接调用 _why() 方法。
        """
        ctx = _make_context(adx=50.0, plus_di=35.0, minus_di=15.0, adx_d3=1.0)
        ok, direction, score, reason = self.strategy._why(ctx)
        assert ok is True
        assert direction == "buy"
        assert 0.4 <= score <= 1.0
        assert "strong_trend" in reason
