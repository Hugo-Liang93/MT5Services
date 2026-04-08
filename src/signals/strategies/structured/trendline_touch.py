"""StructuredTrendlineTouch — 趋势线触碰入场。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ...evaluation.regime import RegimeType
from ...models import SignalContext, SignalDecision
from ..base import get_tf_param
from .base import EntrySpec, EntryType, ExitSpec, HtfPolicy, StructuredStrategyBase, _near_structure_level, _structure_bias_bonus
from .trendline_utils import (
    _bar_value,
    _detect_swing_highs,
    _detect_swing_lows,
    _evaluate_trendline_touch,
    _find_best_trendline,
)


class StructuredTrendlineTouch(StructuredStrategyBase):
    """趋势线三点确认 + HTF 软加分 + Volume 加分。

    方向由趋势线斜率决定（上升趋势线=buy，下降趋势线=sell），
    HTF 方向一致时加分，不一致时降分但不拒绝（小周期可领先大周期）。
    """

    name = "structured_trendline_touch"
    category = "trend"
    htf_policy = HtfPolicy.SOFT_BONUS
    required_indicators = ("atr14", "volume_ratio20")
    htf_required_indicators = ("supertrend14", "adx14")
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
        # 趋势线三点确认本身提供方向信号，不依赖 HTF 决定方向。
        # HTF 一致时加分，不一致时降分但不拒绝。
        htf = self._htf_data(ctx)
        htf_dir = htf.get("supertrend14", {}).get("direction")
        htf_adx = htf.get("adx14", {}).get("adx")

        # direction_hint=None → _when() 双向探测，由趋势线斜率决定方向
        direction_hint: Optional[str] = None

        if htf_dir is None:
            return True, direction_hint, 0.3, "no_htf"

        htf_adx_f = float(htf_adx) if htf_adx is not None else 0.0
        adx_min = get_tf_param(self, "htf_adx_min", ctx.timeframe, self._htf_adx_min)

        if htf_adx is not None and htf_adx_f < adx_min:
            # HTF 弱趋势：不提供方向提示，给基础分
            return True, direction_hint, 0.2, f"htf_weak:{htf_adx}"

        # HTF 有明确方向 → 作为 hint（非强制），加分
        direction_hint = "buy" if int(htf_dir) == 1 else "sell"
        score = min(htf_adx_f / 40.0, 1.0) if htf_adx else 0.3
        return True, direction_hint, score, f"htf:{direction_hint}"

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
        return self._linear_score(self._volume_ratio(ctx), low=0.9, high=1.3)

    def _entry_spec(self, ctx: SignalContext, direction: str) -> EntrySpec:
        if self._last_tl is not None:
            return EntrySpec(
                entry_type=EntryType.LIMIT,
                entry_price=self._last_tl.trendline_price_at_current,
            )
        return EntrySpec()

    _aggression: float = 0.70

    def _exit_spec(self, ctx: SignalContext, direction: str) -> ExitSpec:
        # 趋势线触碰：顺势中等 trail
        aggr = get_tf_param(self, "aggression", ctx.timeframe, self._aggression)
        return ExitSpec(aggression=aggr)

    def _tl_conf(self, candidate: Any, touch_dist: float, tol: float) -> float:
        """趋势线质量评分，返回 0~1。"""
        score = 0.0
        n = len(candidate.covered_points)
        # 覆盖点越多越好（3 点起算，每多 1 点 +0.15，上限 0.45）
        score += min(max(n - self._min_covered_points, 0) * 0.15, 0.45)
        # 跨度越大越好
        span = candidate.covered_points[-1].index - candidate.covered_points[0].index
        if span > 10:
            score += min((span - 10) / 50.0, 0.3)
        # 触碰越近越好
        if tol > 0:
            score += max(1.0 - touch_dist / tol, 0.0) * 0.25
        return min(score, 1.0)

    def evaluate(self, context: SignalContext) -> SignalDecision:
        used = list(self.required_indicators)
        self._last_tl: Any = None
        self._last_dir: Optional[str] = None

        why_ok, direction_hint, why_score, why_reason = self._why(context)
        if not why_ok:
            return self._hold(why_reason, used)

        when_ok, when_score, when_reason = self._when(context, direction_hint)
        if not when_ok:
            return self._hold(when_reason, used)

        direction = self._last_dir or direction_hint
        if direction is None:
            return self._hold("no_direction", used)

        # HTF 方向冲突时降低 why_score
        if direction_hint is not None and direction != direction_hint:
            why_score = max(why_score - 0.3, 0.0)

        where_score, where_info = self._where(context, direction)
        vol_score = self._volume_bonus(context, direction)

        confidence = (
            self._base_confidence
            + min(why_score, 1.0) * self._WHY_BUDGET
            + min(when_score, 1.0) * self._WHEN_BUDGET
            + min(where_score, 1.0) * self._WHERE_BUDGET
            + min(vol_score, 1.0) * self._VOL_BUDGET
        )

        grade = (
            "A"
            if where_score > 0 and vol_score > 0
            else "B" if where_score > 0 or vol_score > 0 else "C"
        )

        reason = f"trendline_{direction}:{when_reason},{why_reason}"
        if where_info:
            reason += f",{where_info}"

        entry_spec = self._entry_spec(context, direction)
        exit_spec = self._exit_spec(context, direction)

        md: Dict[str, Any] = {
            "why": why_reason,
            "when": when_reason,
            "why_score": round(min(why_score, 1.0), 3),
            "when_score": round(min(when_score, 1.0), 3),
            "where_score": round(min(where_score, 1.0), 3),
            "vol_score": round(min(vol_score, 1.0), 3),
            "signal_grade": grade,
            "entry_spec": entry_spec.to_dict(),
            "exit_spec": exit_spec.to_dict(),
        }
        if self._last_tl is not None:
            tl = self._last_tl
            md["trendline_price"] = tl.trendline_price_at_current
            md["slope_per_bar"] = tl.slope_per_bar
            md["covered_points"] = len(tl.covered_points)

        return self._make_decision(direction, confidence, reason, used, metadata=md)
