"""StructuredSessionBreakout — 时段突破。"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext
from ..base import get_tf_param
from .base import StructuredStrategyBase, _structure_bias_bonus, _near_structure_level


class StructuredSessionBreakout(StructuredStrategyBase):
    """亚盘区间形成 → 伦敦/纽约突破 + HTF 同向确认。"""

    name = "structured_session_breakout"
    category = "session"
    required_indicators = ("atr14", "adx14")
    regime_affinity = {
        RegimeType.TRENDING: 0.60,
        RegimeType.RANGING: 0.30,
        RegimeType.BREAKOUT: 1.00,
        RegimeType.UNCERTAIN: 0.40,
    }

    _penetration_min_atr: float = 0.10
    _asia_range_min_atr: float = 0.3
    _asia_range_max_atr: float = 2.5
    _adx_d3_min: float = 0.8

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        ms = self._ms(ctx)
        session = ms.get("current_session", "")
        if session not in ("london", "new_york"):
            return False, None, 0, f"session:{session}"

        asia_high = ms.get("asia_range_high")
        asia_low = ms.get("asia_range_low")
        if asia_high is None or asia_low is None:
            return False, None, 0, "no_asia"

        close = self._close(ctx)
        atr = self._atr(ctx)
        if close is None or atr is None or atr <= 0:
            return False, None, 0, "no_data"

        tf = ctx.timeframe
        ah, al = float(asia_high), float(asia_low)
        asia_range = ah - al
        range_min = get_tf_param(self, "asia_range_min_atr", tf, self._asia_range_min_atr)
        range_max = get_tf_param(self, "asia_range_max_atr", tf, self._asia_range_max_atr)
        if asia_range < atr * range_min or asia_range > atr * range_max:
            return False, None, 0, f"range_bad:{asia_range:.0f}"

        # 方向判定
        if close > ah:
            pen = (close - ah) / atr
            direction = "buy"
        elif close < al:
            pen = (al - close) / atr
            direction = "sell"
        else:
            return False, None, 0, "inside_range"

        pen_min = get_tf_param(self, "penetration_min_atr", tf, self._penetration_min_atr)
        if pen < pen_min:
            return False, None, 0, f"weak:{pen:.3f}"

        # HTF 确认
        htf = self._htf_data(ctx)
        htf_dir = htf.get("supertrend14", {}).get("direction")
        if htf_dir is not None:
            if direction == "buy" and int(htf_dir) != 1:
                return False, None, 0, "htf_conflict"
            if direction == "sell" and int(htf_dir) != -1:
                return False, None, 0, "htf_conflict"

        pen_score = min(pen / 0.5, 1.0)  # 0.5 ATR 穿透 = 满分
        htf_score = 0.4 if htf_dir is not None else 0.0
        score = min(pen_score * 0.6 + htf_score, 1.0)
        return (
            True,
            direction,
            score,
            f"asia_break:{direction},pen={pen:.2f}",
        )

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        ad = self._adx_full(ctx)
        d3 = ad["adx_d3"]
        adx_d3_min = get_tf_param(self, "adx_d3_min", ctx.timeframe, self._adx_d3_min)
        if d3 is not None and d3 < adx_d3_min:
            return False, 0, f"adx_flat:d3={d3:.1f}"
        # ADX 上升动量越强 score 越高
        score = min(float(d3 or 1.0) / 3.0, 1.0)
        return True, score, "adx_rising"

    def _where(self, ctx: SignalContext, direction: str) -> Tuple[float, str]:
        ms = self._ms(ctx)
        breakout = ms.get("breakout_state", "none")
        if "asia" in str(breakout):
            return 1.0, f"break={breakout}"
        compression = ms.get("compression_state", "unknown")
        if compression == "contracted":
            return 0.6, "compressed"
        return 0.0, ""

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:  # type: ignore[override]
        return self._linear_score(self._volume_ratio(ctx), low=1.0, high=1.5)

    def _entry_spec(self, ctx: SignalContext, direction: str) -> Dict[str, Any]:
        return {"entry_type": "market", "entry_price": None, "entry_zone_atr": 0.3}

    _aggression: float = 0.60

    def _exit_spec(self, ctx: SignalContext, direction: str) -> Dict[str, Any]:
        # 时段突破：中等 trail
        aggr = get_tf_param(self, "aggression", ctx.timeframe, self._aggression)
        return {"aggression": aggr, "sl_atr": None, "tp_atr": None}
