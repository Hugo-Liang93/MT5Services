"""StructuredSweepReversal — 扫单反转。"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from ..base import get_tf_param
from .base import StructuredStrategyBase, _structure_bias_bonus, _near_structure_level


class StructuredSweepReversal(StructuredStrategyBase):
    """RSI 从极端区回归 + ADX 不强 + sweep/reclaim 结构加分。"""

    name = "structured_sweep_reversal"
    category = "price_action"
    required_indicators = ("rsi14", "atr14", "adx14")
    regime_affinity = {
        RegimeType.TRENDING: 0.30,
        RegimeType.RANGING: 0.90,
        RegimeType.BREAKOUT: 0.50,
        RegimeType.UNCERTAIN: 0.70,
    }

    _rsi_extreme_low: float = 28.0
    _rsi_extreme_high: float = 72.0
    _adx_max: float = 28.0
    _base_confidence: float = 0.48

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        adx_data = self._adx_full(ctx)
        adx = adx_data["adx"]
        if adx is not None and adx > get_tf_param(
            self, "adx_max", ctx.timeframe, self._adx_max
        ):
            return False, None, 0, f"adx_high:{adx:.0f}"

        rsi, rsi_d3 = self._rsi(ctx)
        if rsi is None:
            return False, None, 0, "no_rsi"

        if rsi <= self._rsi_extreme_low:
            # 超卖 → 潜在买入反转
            if rsi_d3 is not None and rsi_d3 < 0:
                return False, None, 0, "still_falling"
            return True, "buy", 0.08, f"oversold:{rsi:.0f}"
        elif rsi >= self._rsi_extreme_high:
            if rsi_d3 is not None and rsi_d3 > 0:
                return False, None, 0, "still_rising"
            return True, "sell", 0.08, f"overbought:{rsi:.0f}"

        return False, None, 0, f"rsi_mid:{rsi:.0f}"

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        # RSI 极端本身就是时机条件，Why 已检查。这里做 bar 形态二次确认。
        bs = ctx.indicators.get("bar_stats20", {})
        close_pos = bs.get("close_position")
        if close_pos is not None:
            cp = float(close_pos)
            if direction == "buy" and cp < 0.40:
                return False, 0, f"bearish_bar:{cp:.2f}"
            if direction == "sell" and cp > 0.60:
                return False, 0, f"bullish_bar:{cp:.2f}"
        return True, 0.05, "bar_ok"

    def _where(self, ctx: SignalContext, direction: str) -> Tuple[float, str]:
        ms = self._ms(ctx)
        sweep = ms.get("sweep_confirmation_state", "none")
        reclaim = ms.get("reclaim_state", "none")

        if direction == "buy":
            if str(sweep).startswith("bullish_"):
                return 0.15, f"sweep={sweep}"
            if str(reclaim).startswith("bullish_reclaim"):
                return 0.10, f"reclaim={reclaim}"
        else:
            if str(sweep).startswith("bearish_"):
                return 0.15, f"sweep={sweep}"
            if str(reclaim).startswith("bearish_reclaim"):
                return 0.10, f"reclaim={reclaim}"

        return 0.0, ""

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:
        vr = self._volume_ratio(ctx)
        return 0.08 if vr is not None and vr > 2.0 else 0.0
