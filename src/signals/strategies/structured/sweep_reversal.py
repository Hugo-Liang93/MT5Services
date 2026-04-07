"""StructuredSweepReversal — 扫单反转。"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from ..base import get_tf_param
from .base import StructuredStrategyBase, _near_structure_level, _structure_bias_bonus


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
            if rsi_d3 is not None and rsi_d3 < 0:
                return False, None, 0, "still_falling"
            # RSI 越极端，score 越高
            score = min((self._rsi_extreme_low - rsi) / 15.0 + 0.5, 1.0)
            return True, "buy", score, f"oversold:{rsi:.0f}"
        elif rsi >= self._rsi_extreme_high:
            if rsi_d3 is not None and rsi_d3 > 0:
                return False, None, 0, "still_rising"
            score = min((rsi - self._rsi_extreme_high) / 15.0 + 0.5, 1.0)
            return True, "sell", score, f"overbought:{rsi:.0f}"

        return False, None, 0, f"rsi_mid:{rsi:.0f}"

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        bs = ctx.indicators.get("bar_stats20", {})
        close_pos = bs.get("close_position")
        if close_pos is not None:
            cp = float(close_pos)
            if direction == "buy" and cp < 0.40:
                return False, 0, f"bearish_bar:{cp:.2f}"
            if direction == "sell" and cp > 0.60:
                return False, 0, f"bullish_bar:{cp:.2f}"
            # 连续评分：收盘越偏反转方向越好
            if direction == "buy":
                score = min(cp / 0.8, 1.0)
            else:
                score = min((1.0 - cp) / 0.8, 1.0)
            return True, score, f"bar:{cp:.2f}"
        return True, 0.5, "bar_ok"

    def _where(self, ctx: SignalContext, direction: str) -> Tuple[float, str]:
        ms = self._ms(ctx)
        sweep = ms.get("sweep_confirmation_state", "none")
        reclaim = ms.get("reclaim_state", "none")

        if direction == "buy":
            if str(sweep).startswith("bullish_"):
                return 1.0, f"sweep={sweep}"
            if str(reclaim).startswith("bullish_reclaim"):
                return 0.7, f"reclaim={reclaim}"
        else:
            if str(sweep).startswith("bearish_"):
                return 1.0, f"sweep={sweep}"
            if str(reclaim).startswith("bearish_reclaim"):
                return 0.7, f"reclaim={reclaim}"

        return 0.0, ""

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:
        return self._linear_score(self._volume_ratio(ctx), low=1.4, high=2.0)

    def _entry_spec(self, ctx: SignalContext, direction: str) -> Dict[str, Any]:
        return {"entry_type": "market", "entry_price": None, "entry_zone_atr": 0.3}

    _aggression: float = 0.20

    def _exit_spec(self, ctx: SignalContext, direction: str) -> Dict[str, Any]:
        # 反转：紧 trail 快速锁利
        aggr = get_tf_param(self, "aggression", ctx.timeframe, self._aggression)
        return {"aggression": aggr, "sl_atr": 1.2, "tp_atr": 2.0}
