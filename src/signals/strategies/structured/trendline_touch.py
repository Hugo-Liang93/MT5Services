"""StructuredTrendlineTouch — 趋势线触碰入场。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext, SignalDecision
from ..base import get_tf_param
from .base import StructuredStrategyBase, _structure_bias_bonus, _near_structure_level

from ..legacy.trendline import (
    _bar_value,
    _detect_swing_highs,
    _detect_swing_lows,
    _evaluate_trendline_touch,
    _find_best_trendline,
)


class StructuredTrendlineTouch(StructuredStrategyBase):
    """趋势线三点确认 + HTF 方向 + Volume 加分。

    复用 trendline.py 的 swing 检测和趋势线拟合纯函数，
    在 StructuredStrategyBase 框架中增加 HTF 确认和 Volume 加分。
    """

    name = "structured_trendline_touch"
    category = "trend"
    required_indicators = ("atr14",)
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING: 1.00,
        RegimeType.RANGING: 0.15,
        RegimeType.BREAKOUT: 0.50,
        RegimeType.UNCERTAIN: 0.40,
    }

    _htf_adx_min: float = 16.0
    _swing_window: int = 3
    _min_swing_spacing: int = 5
    _touch_tolerance_atr: float = 0.3
    _min_slope_atr: float = 0.01
    _max_slope_atr: float = 0.15
    _fit_tolerance_atr: float = 0.5
    _min_covered_points: int = 3

    def __init__(
        self,
        name: Optional[str] = None,
        htf: str = "H1",
        lookback_bars: int = 80,
    ) -> None:
        super().__init__(name=name, htf=htf)
        self.recent_bars_depth: int = lookback_bars

    def _why(self, ctx: SignalContext) -> Tuple[bool, Optional[str], float, str]:
        htf = self._htf_data(ctx)
        htf_dir = htf.get("supertrend14", {}).get("direction")
        htf_adx = htf.get("adx14", {}).get("adx")

        if htf_dir is None:
            return True, None, 0, "no_htf"
        if htf_adx is not None and float(htf_adx) < self._htf_adx_min:
            return True, None, 0, f"htf_weak:{htf_adx}"

        direction_hint = "buy" if int(htf_dir) == 1 else "sell"
        adx_bonus = min(float(htf_adx or 20) / 40.0 * 0.10, 0.10) if htf_adx else 0.0
        return True, direction_hint, adx_bonus, f"htf:{direction_hint}"

    def _when(self, ctx: SignalContext, direction: str) -> Tuple[bool, float, str]:
        atr = self._atr(ctx)
        if atr is None or atr <= 0:
            return False, 0, "no_atr"

        bars = ctx.metadata.get("recent_bars")
        if not isinstance(bars, (list, tuple)) or len(bars) < 15:
            return False, 0, "no_bars"

        tf = ctx.timeframe
        sw = int(get_tf_param(self, "swing_window", tf, float(self._swing_window)))
        sp = int(
            get_tf_param(self, "min_swing_spacing", tf, float(self._min_swing_spacing))
        )
        tol = get_tf_param(self, "touch_tolerance_atr", tf, self._touch_tolerance_atr)

        cur_high = _bar_value(bars[-1], "high")
        cur_low = _bar_value(bars[-1], "low")
        cur_close = _bar_value(bars[-1], "close")
        if None in {cur_high, cur_low, cur_close}:
            return False, 0, "no_bar_data"

        idx = len(bars) - 1
        tolerance = tol * atr
        bar_list = list(bars)

        if direction in ("buy", None):
            lows = _detect_swing_lows(bar_list, sw, sp)
            asc = _find_best_trendline(
                lows,
                atr,
                idx,
                direction="ascending",
                min_slope_atr=self._min_slope_atr,
                max_slope_atr=self._max_slope_atr,
                fit_tolerance_atr=self._fit_tolerance_atr,
                min_covered_points=self._min_covered_points,
            )
            if asc is not None:
                touch = _evaluate_trendline_touch(
                    direction="buy",
                    trendline_price=asc.trendline_price_at_current,
                    touch_price=cur_low,
                    close_price=cur_close,
                    tolerance=tolerance,
                )
                if touch.is_valid:
                    conf = self._tl_conf(asc, touch.touch_distance, tolerance)
                    self._last_tl = asc
                    self._last_dir = "buy"
                    return True, conf, f"asc:pts={len(asc.covered_points)}"

        if direction in ("sell", None):
            highs = _detect_swing_highs(bar_list, sw, sp)
            desc = _find_best_trendline(
                highs,
                atr,
                idx,
                direction="descending",
                min_slope_atr=self._min_slope_atr,
                max_slope_atr=self._max_slope_atr,
                fit_tolerance_atr=self._fit_tolerance_atr,
                min_covered_points=self._min_covered_points,
            )
            if desc is not None:
                touch = _evaluate_trendline_touch(
                    direction="sell",
                    trendline_price=desc.trendline_price_at_current,
                    touch_price=cur_high,
                    close_price=cur_close,
                    tolerance=tolerance,
                )
                if touch.is_valid:
                    conf = self._tl_conf(desc, touch.touch_distance, tolerance)
                    self._last_tl = desc
                    self._last_dir = "sell"
                    return True, conf, f"desc:pts={len(desc.covered_points)}"

        return False, 0, "no_touch"

    def _where(self, ctx: SignalContext, direction: str) -> Tuple[float, str]:
        return _structure_bias_bonus(self._ms(ctx), direction)

    def _volume_bonus(self, ctx: SignalContext, direction: str) -> float:
        vr = self._volume_ratio(ctx)
        return 0.05 if vr is not None and vr > 1.3 else 0.0

    def _tl_conf(self, candidate: Any, touch_dist: float, tol: float) -> float:
        conf = 0.0
        n = len(candidate.covered_points)
        conf += min(max(n - self._min_covered_points, 0) * 0.04, 0.12)
        span = candidate.covered_points[-1].index - candidate.covered_points[0].index
        if span > 10:
            conf += min((span - 10) / 50.0 * 0.08, 0.08)
        if tol > 0:
            conf += max(1.0 - touch_dist / tol, 0.0) * 0.06
        return conf

    def evaluate(self, context: SignalContext) -> SignalDecision:
        used = list(self.required_indicators)
        self._last_tl: Any = None
        self._last_dir: Optional[str] = None

        why_ok, direction_hint, why_conf, why_reason = self._why(context)

        when_ok, when_conf, when_reason = self._when(context, direction_hint)
        if not when_ok:
            return self._hold(when_reason, used)

        direction = self._last_dir or direction_hint
        if direction is None:
            return self._hold("no_direction", used)

        htf_penalty = 0.0
        if direction_hint is not None and direction != direction_hint:
            htf_penalty = -0.08

        where_bonus, where_info = self._where(context, direction)
        vol_bonus = self._volume_bonus(context, direction)

        confidence = (
            self._base_confidence
            + why_conf
            + when_conf
            + where_bonus
            + vol_bonus
            + htf_penalty
        )
        reason = f"trendline_{direction}:{when_reason},{why_reason}"
        if where_info:
            reason += f",{where_info}"

        md: Dict[str, Any] = {
            "why": why_reason,
            "when": when_reason,
            "where_bonus": where_bonus,
            "vol_bonus": vol_bonus,
        }
        if self._last_tl is not None:
            tl = self._last_tl
            md["trendline_price"] = tl.trendline_price_at_current
            md["slope_per_bar"] = tl.slope_per_bar
            md["covered_points"] = len(tl.covered_points)

        return self._make_decision(direction, confidence, reason, used, metadata=md)
