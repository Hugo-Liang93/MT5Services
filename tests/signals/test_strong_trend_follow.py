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
