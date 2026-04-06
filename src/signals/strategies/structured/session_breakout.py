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

        ah, al = float(asia_high), float(asia_low)
        asia_range = ah - al
        if (
            asia_range < atr * self._asia_range_min_atr
            or asia_range > atr * self._asia_range_max_atr
        ):
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

        if pen < self._penetration_min_atr:
            return False, None, 0, f"weak:{pen:.3f}"

        # HTF 确认
        htf = self._htf_data(ctx)
        htf_dir = htf.get("supertrend14", {}).get("direction")
        if htf_dir is not None:
            if direction == "buy" and int(htf_dir) != 1:
                return False, None, 0, "htf_conflict"
            if direction == "sell" and int(htf_dir) != -1:
                return False, None, 0, "htf_conflict"

        pen_bonus = min(pen * 0.12, 0.12)
        htf_bonus = 0.08 if htf_dir is not None else 0.0
        return (
            True,
            direction,
            pen_bonus + htf_bonus,
            f"asia_break:{direction},pen={pen:.2f}",
        )

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        ad = self._adx_full(ctx)
        d3 = ad["adx_d3"]
        if d3 is not None and d3 < self._adx_d3_min:
            return False, 0, f"adx_flat:d3={d3:.1f}"
        return True, 0.03, "adx_rising"

    def _where(self, ctx: SignalContext, direction: str) -> Tuple[float, str]:
        ms = self._ms(ctx)
        breakout = ms.get("breakout_state", "none")
        if "asia" in str(breakout):
            return 0.08, f"break={breakout}"
        compression = ms.get("compression_state", "unknown")
        if compression == "contracted":
            return 0.05, "compressed"
        return 0.0, ""

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:  # type: ignore[override]
        vr = self._volume_ratio(ctx)
        return 0.05 if vr is not None and vr > 1.5 else 0.0
