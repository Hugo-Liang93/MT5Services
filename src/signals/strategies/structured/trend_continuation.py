"""StructuredTrendContinuation — 趋势回调入场。"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from ..base import get_tf_param
from .base import (
    EntrySpec,
    ExitSpec,
    HtfPolicy,
    StructuredStrategyBase,
    _near_structure_level,
    _structure_bias_bonus,
)


class StructuredTrendContinuation(StructuredStrategyBase):
    """HTF 趋势方向 + LTF RSI 回调区间 + 结构位加分。"""

    name = "structured_trend_continuation"
    category = "multi_tf"
    htf_policy = HtfPolicy.HARD_GATE
    required_indicators = ("rsi14", "atr14")
    regime_affinity = {
        RegimeType.TRENDING: 1.00,
        RegimeType.RANGING: 0.05,
        RegimeType.BREAKOUT: 0.20,
        RegimeType.UNCERTAIN: 0.10,
    }

    _rsi_buy_low: float = 30.0
    _rsi_buy_high: float = 50.0
    _rsi_sell_low: float = 50.0
    _rsi_sell_high: float = 70.0
    _htf_adx_min: float = 20.0

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        htf = self._htf_data(ctx)
        htf_dir = htf.get("supertrend14", {}).get("direction")
        htf_adx = htf.get("adx14", {}).get("adx")
        htf_ema = htf.get("ema50", {}).get("ema")

        if htf_dir is None or htf_adx is None:
            return False, None, 0, "no_htf"

        htf_dir_i = int(htf_dir)
        htf_adx_f = float(htf_adx)
        if htf_adx_f < get_tf_param(
            self, "htf_adx_min", ctx.timeframe, self._htf_adx_min
        ):
            return False, None, 0, f"htf_adx_low:{htf_adx_f:.0f}"

        direction = "buy" if htf_dir_i == 1 else "sell"

        # EMA 顺势检查
        close = self._close(ctx)
        if close is not None and htf_ema is not None:
            if direction == "buy" and close < float(htf_ema):
                return False, None, 0, "below_ema"
            if direction == "sell" and close > float(htf_ema):
                return False, None, 0, "above_ema"

        score = min(htf_adx_f / 40.0, 1.0)
        return True, direction, score, f"htf:{direction},adx={htf_adx_f:.0f}"

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        tf = ctx.timeframe
        rsi, rsi_d3 = self._rsi(ctx)
        if rsi is None:
            return False, 0, "no_rsi"

        if direction == "buy":
            lo = get_tf_param(self, "rsi_buy_low", tf, self._rsi_buy_low)
            hi = get_tf_param(self, "rsi_buy_high", tf, self._rsi_buy_high)
            if not (lo <= rsi <= hi):
                return False, 0, f"rsi={rsi:.0f}∉[{lo:.0f},{hi:.0f}]"
            if rsi_d3 is not None and rsi_d3 < -3.0:
                return False, 0, f"rsi_falling:d3={rsi_d3:.1f}"
        else:
            lo = get_tf_param(self, "rsi_sell_low", tf, self._rsi_sell_low)
            hi = get_tf_param(self, "rsi_sell_high", tf, self._rsi_sell_high)
            if not (lo <= rsi <= hi):
                return False, 0, f"rsi={rsi:.0f}∉[{lo:.0f},{hi:.0f}]"
            if rsi_d3 is not None and rsi_d3 > 3.0:
                return False, 0, f"rsi_rising:d3={rsi_d3:.1f}"

        center = (lo + hi) / 2
        score = max(0.0, 1.0 - abs(rsi - center) / 20.0)
        return True, score, f"rsi={rsi:.0f}"

    def _where(self, ctx: SignalContext, direction: str) -> Tuple[float, str]:
        return _structure_bias_bonus(self._ms(ctx), direction)

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:
        return self._linear_score(self._volume_ratio(ctx), low=1.0, high=1.5)

    def _entry_spec(self, ctx: SignalContext, direction: str) -> EntrySpec:
        return EntrySpec()

    _aggression: float = 0.80

    def _exit_spec(self, ctx: SignalContext, direction: str) -> ExitSpec:
        # 趋势回调：宽 trail 让利润奔跑
        aggr = get_tf_param(self, "aggression", ctx.timeframe, self._aggression)
        return ExitSpec(aggression=aggr)
