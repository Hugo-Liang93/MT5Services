"""StructuredBreakoutFollow — 突破跟随。"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from ..base import get_tf_param
from .base import StructuredStrategyBase, _structure_bias_bonus, _near_structure_level


class StructuredBreakoutFollow(StructuredStrategyBase):
    """ADX 上升 + DI 方向一致 + HTF 确认 + 结构突破加分。"""

    name = "structured_breakout_follow"
    category = "breakout"
    required_indicators = ("adx14", "rsi14", "atr14")
    regime_affinity = {
        RegimeType.TRENDING: 0.70,
        RegimeType.RANGING: 0.20,
        RegimeType.BREAKOUT: 1.00,
        RegimeType.UNCERTAIN: 0.50,
    }

    _adx_min: float = 18.0
    _adx_max: float = 38.0
    _adx_d3_min: float = 1.0
    _di_diff_min: float = 3.0
    _rsi_max_buy: float = 74.0
    _rsi_min_sell: float = 26.0

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        ad = self._adx_full(ctx)
        adx, d3 = ad["adx"], ad["adx_d3"]
        plus_di, minus_di = ad["plus_di"], ad["minus_di"]

        if adx is None:
            return False, None, 0, "no_adx"
        if adx < self._adx_min:
            return False, None, 0, f"adx_low:{adx:.0f}"
        if adx > self._adx_max:
            return False, None, 0, f"adx_over:{adx:.0f}"
        if d3 is None or d3 < self._adx_d3_min:
            return False, None, 0, f"adx_flat:d3={d3}"

        di_spread = (plus_di or 0) - (minus_di or 0)
        if abs(di_spread) < self._di_diff_min:
            return False, None, 0, f"di_flat:{di_spread:.1f}"

        direction = "buy" if di_spread > 0 else "sell"

        # HTF 确认
        htf = self._htf_data(ctx)
        htf_dir = htf.get("supertrend14", {}).get("direction")
        if htf_dir is not None:
            if direction == "buy" and int(htf_dir) != 1:
                return False, None, 0, "htf_conflict"
            if direction == "sell" and int(htf_dir) != -1:
                return False, None, 0, "htf_conflict"

        # RSI 非极端
        rsi, _ = self._rsi(ctx)
        if rsi is not None:
            if direction == "buy" and rsi > self._rsi_max_buy:
                return False, None, 0, f"rsi_hot:{rsi:.0f}"
            if direction == "sell" and rsi < self._rsi_min_sell:
                return False, None, 0, f"rsi_cold:{rsi:.0f}"

        rise_bonus = min(d3 / 8.0 * 0.12, 0.12)
        return True, direction, rise_bonus, f"adx={adx:.0f},d3={d3:.1f}"

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        # ADX + DI 已在 why 中验证，when 检查 bar 力度
        bs = ctx.indicators.get("bar_stats20", {})
        br = bs.get("body_ratio")
        bonus = 0.05 if br is not None and float(br) > 1.2 else 0.0
        return True, bonus, f"body={br}"

    def _where(self, ctx: SignalContext, direction: str) -> Tuple[float, str]:
        ms = self._ms(ctx)
        breakout = ms.get("breakout_state", "none")
        breached = ms.get("breached_levels", [])

        if direction == "buy" and str(breakout).startswith("above_") and breached:
            return 0.10, f"break={breakout}"
        if direction == "sell" and str(breakout).startswith("below_") and breached:
            return 0.10, f"break={breakout}"

        fp = ms.get("first_pullback_state", "none")
        if direction == "buy" and str(fp).startswith("bullish_"):
            return 0.08, f"pullback={fp}"
        if direction == "sell" and str(fp).startswith("bearish_"):
            return 0.08, f"pullback={fp}"

        return 0.0, ""

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:
        vr = self._volume_ratio(ctx)
        return 0.08 if vr is not None and vr > 1.8 else 0.0
