"""最佳拟合趋势线策略。

在最近 N 根 bar 中提取全部 swing low / swing high，
搜索能覆盖尽量多 swing 点的支撑 / 压力趋势线，
再等待当前价格轻触趋势线附近时顺势入场。

仅在 H1/H4 级别运行（低 TF swing 点不稳定，趋势线不可靠）。
斜率以 ATR 归一化，过滤太平（横盘）和太陡（不可持续）的线。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..evaluation.regime import RegimeType
from ..models import SignalContext, SignalDecision
from .base import _resolve_indicator_value, get_tf_param

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SwingPoint:
    """检测到的 swing high/low 极值点。"""

    index: int
    price: float


@dataclass(frozen=True)
class TrendlineTouch:
    """当前 bar 相对趋势线的触碰结果。"""

    is_valid: bool
    touch_distance: float


@dataclass(frozen=True)
class TrendlineCandidate:
    """由全部 swing 点拟合出的候选趋势线。"""

    direction: str
    anchor1: SwingPoint
    anchor2: SwingPoint
    covered_points: tuple[SwingPoint, ...]
    slope_per_bar: float
    normalized_slope: float
    mean_error_atr: float
    max_error_atr: float
    trendline_price_at_current: float
    score: tuple[int, int, int, float, float, int]


def _bar_value(bar: Any, field: str) -> Optional[float]:
    value = getattr(bar, field, None)
    if value is None and isinstance(bar, dict):
        value = bar.get(field)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _project_price(anchor: SwingPoint, slope_per_bar: float, index: int) -> float:
    return anchor.price + slope_per_bar * (index - anchor.index)


def _evaluate_trendline_touch(
    *,
    direction: str,
    trendline_price: float,
    touch_price: float,
    close_price: float,
    tolerance: float,
) -> TrendlineTouch:
    """评估当前 bar 是否属于“轻触趋势线附近”。

    轻触要求：
    - 触点必须落在趋势线双边容差区间内；
    - 收盘价仍需站在线的顺势一侧。
    """
    touch_distance = abs(touch_price - trendline_price)
    within_band = touch_distance <= tolerance if tolerance > 0 else touch_distance == 0.0

    if direction == "buy":
        is_valid = within_band and close_price > trendline_price
    elif direction == "sell":
        is_valid = within_band and close_price < trendline_price
    else:
        is_valid = False

    return TrendlineTouch(is_valid=is_valid, touch_distance=touch_distance)


def _detect_swing_lows(
    bars: List[Any],
    window: int,
    min_spacing: int,
) -> List[SwingPoint]:
    """检测 swing low。"""
    swings: List[SwingPoint] = []
    n = len(bars)

    for i in range(window, n - window):
        center_low = _bar_value(bars[i], "low")
        if center_low is None:
            continue

        is_swing = True
        for j in range(i - window, i + window + 1):
            if j == i:
                continue
            neighbor = _bar_value(bars[j], "low")
            if neighbor is None or neighbor < center_low:
                is_swing = False
                break

        if not is_swing:
            continue

        if swings and (i - swings[-1].index) < min_spacing:
            if center_low < swings[-1].price:
                swings[-1] = SwingPoint(index=i, price=center_low)
            continue

        swings.append(SwingPoint(index=i, price=center_low))

    return swings


def _detect_swing_highs(
    bars: List[Any],
    window: int,
    min_spacing: int,
) -> List[SwingPoint]:
    """检测 swing high。"""
    swings: List[SwingPoint] = []
    n = len(bars)

    for i in range(window, n - window):
        center_high = _bar_value(bars[i], "high")
        if center_high is None:
            continue

        is_swing = True
        for j in range(i - window, i + window + 1):
            if j == i:
                continue
            neighbor = _bar_value(bars[j], "high")
            if neighbor is None or neighbor > center_high:
                is_swing = False
                break

        if not is_swing:
            continue

        if swings and (i - swings[-1].index) < min_spacing:
            if center_high > swings[-1].price:
                swings[-1] = SwingPoint(index=i, price=center_high)
            continue

        swings.append(SwingPoint(index=i, price=center_high))

    return swings


def _candidate_score(
    *,
    covered_count: int,
    span: int,
    violation_count: int,
    mean_error_atr: float,
    max_error_atr: float,
    latest_covered_index: int,
) -> tuple[int, int, int, float, float, int]:
    """趋势线排序规则。

    优先级：
    1. 覆盖 swing 点数量更多
    2. 时间跨度更长
    3. 违例更少
    4. 平均偏差更小
    5. 最大偏差更小
    6. 最近覆盖点更靠近当前
    """
    return (
        covered_count,
        span,
        -violation_count,
        -mean_error_atr,
        -max_error_atr,
        latest_covered_index,
    )


def _find_best_trendline(
    points: Sequence[SwingPoint],
    atr: float,
    current_bar_index: int,
    *,
    direction: str,
    min_slope_atr: float = 0.01,
    max_slope_atr: float = 0.15,
    fit_tolerance_atr: float = 0.5,
    min_covered_points: int = 3,
    max_break_violations: int = 0,
) -> Optional[TrendlineCandidate]:
    """在全部 swing 点中搜索覆盖度最高的趋势线。"""
    if len(points) < min_covered_points or atr <= 0:
        return None

    tolerance = fit_tolerance_atr * atr
    best: Optional[TrendlineCandidate] = None

    for left in range(len(points) - 1):
        anchor1 = points[left]
        for right in range(left + 1, len(points)):
            anchor2 = points[right]
            if anchor2.index <= anchor1.index:
                continue

            if direction == "ascending" and anchor2.price <= anchor1.price:
                continue
            if direction == "descending" and anchor2.price >= anchor1.price:
                continue

            slope = (anchor2.price - anchor1.price) / (anchor2.index - anchor1.index)
            normalized_slope = abs(slope) / atr
            if normalized_slope < min_slope_atr or normalized_slope > max_slope_atr:
                continue

            covered_points: list[SwingPoint] = []
            errors_atr: list[float] = []
            violation_count = 0

            for point in points:
                expected = _project_price(anchor1, slope, point.index)
                error = abs(point.price - expected)
                if error <= tolerance:
                    covered_points.append(point)
                    errors_atr.append(error / atr)
                    continue

                if direction == "ascending" and point.price < expected - tolerance:
                    violation_count += 1
                elif direction == "descending" and point.price > expected + tolerance:
                    violation_count += 1

            if len(covered_points) < min_covered_points:
                continue
            if violation_count > max_break_violations:
                continue

            covered_points = sorted(covered_points, key=lambda item: item.index)
            mean_error_atr = sum(errors_atr) / len(errors_atr)
            max_error_atr = max(errors_atr) if errors_atr else 0.0
            span = covered_points[-1].index - covered_points[0].index
            score = _candidate_score(
                covered_count=len(covered_points),
                span=span,
                violation_count=violation_count,
                mean_error_atr=mean_error_atr,
                max_error_atr=max_error_atr,
                latest_covered_index=covered_points[-1].index,
            )

            candidate = TrendlineCandidate(
                direction=direction,
                anchor1=anchor1,
                anchor2=anchor2,
                covered_points=tuple(covered_points),
                slope_per_bar=slope,
                normalized_slope=normalized_slope,
                mean_error_atr=mean_error_atr,
                max_error_atr=max_error_atr,
                trendline_price_at_current=_project_price(anchor1, slope, current_bar_index),
                score=score,
            )

            if best is None or candidate.score > best.score:
                best = candidate

    return best


def _covered_points_payload(points: Iterable[SwingPoint]) -> list[dict[str, float | int]]:
    return [
        {"index": point.index, "price": point.price}
        for point in points
    ]


class TrendlineThreeTouchStrategy:
    """趋势线最佳拟合入场策略。

    在最近 lookback bars 的 swing 点中，搜索覆盖度最高的趋势线，
    再等待当前 bar 轻触趋势线时顺势入场。
    """

    name = "trendline_3touch"
    category = "trend"
    required_indicators = ("atr14",)
    preferred_scopes = ("confirmed",)
    regime_affinity = {
        RegimeType.TRENDING: 1.00,
        RegimeType.RANGING: 0.15,
        RegimeType.BREAKOUT: 0.50,
        RegimeType.UNCERTAIN: 0.40,
    }

    _swing_window: int = 3
    _min_swing_spacing: int = 5
    _touch_tolerance_atr: float = 0.3
    _min_slope_atr: float = 0.01
    _max_slope_atr: float = 0.15
    _fit_tolerance_atr: float = 0.5
    _min_covered_points: int = 3

    def __init__(self, *, lookback_bars: int = 80) -> None:
        self.recent_bars_depth: int = lookback_bars

    def evaluate(self, context: SignalContext) -> SignalDecision:
        used: List[str] = ["atr14"]
        hold = SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction="hold",
            confidence=0.0,
            reason="no_trendline_setup",
            used_indicators=used,
        )

        atr, _ = _resolve_indicator_value(
            context.indicators, (("atr14", "atr"), ("atr", "atr"))
        )
        if atr is None or atr <= 0:
            return hold

        bars = context.metadata.get("recent_bars")
        if not isinstance(bars, (list, tuple)) or len(bars) < 15:
            return hold

        tf = context.timeframe
        swing_window = int(get_tf_param(self, "swing_window", tf, float(self._swing_window)))
        min_spacing = int(get_tf_param(self, "min_swing_spacing", tf, float(self._min_swing_spacing)))
        touch_tol = get_tf_param(self, "touch_tolerance_atr", tf, self._touch_tolerance_atr)
        min_slope = get_tf_param(self, "min_slope_atr", tf, self._min_slope_atr)
        max_slope = get_tf_param(self, "max_slope_atr", tf, self._max_slope_atr)
        fit_tol = get_tf_param(self, "fit_tolerance_atr", tf, self._fit_tolerance_atr)

        cur_high = _bar_value(bars[-1], "high")
        cur_low = _bar_value(bars[-1], "low")
        cur_close = _bar_value(bars[-1], "close")
        if None in {cur_high, cur_low, cur_close}:
            return hold

        current_idx = len(bars) - 1
        swing_lows = _detect_swing_lows(list(bars), swing_window, min_spacing)
        swing_highs = _detect_swing_highs(list(bars), swing_window, min_spacing)

        ascending = _find_best_trendline(
            swing_lows,
            atr,
            current_idx,
            direction="ascending",
            min_slope_atr=min_slope,
            max_slope_atr=max_slope,
            fit_tolerance_atr=fit_tol,
            min_covered_points=self._min_covered_points,
        )
        descending = _find_best_trendline(
            swing_highs,
            atr,
            current_idx,
            direction="descending",
            min_slope_atr=min_slope,
            max_slope_atr=max_slope,
            fit_tolerance_atr=fit_tol,
            min_covered_points=self._min_covered_points,
        )

        candidates: list[tuple[str, float, TrendlineCandidate, str, TrendlineTouch]] = []

        if ascending is not None:
            tolerance = touch_tol * atr
            touch = _evaluate_trendline_touch(
                direction="buy",
                trendline_price=ascending.trendline_price_at_current,
                touch_price=cur_low,
                close_price=cur_close,
                tolerance=tolerance,
            )
            if touch.is_valid:
                conf = self._compute_confidence(ascending, touch.touch_distance, tolerance)
                candidates.append(("buy", conf, ascending, "ascending_trendline_touch", touch))

        if descending is not None:
            tolerance = touch_tol * atr
            touch = _evaluate_trendline_touch(
                direction="sell",
                trendline_price=descending.trendline_price_at_current,
                touch_price=cur_high,
                close_price=cur_close,
                tolerance=tolerance,
            )
            if touch.is_valid:
                conf = self._compute_confidence(descending, touch.touch_distance, tolerance)
                candidates.append(("sell", conf, descending, "descending_trendline_touch", touch))

        if not candidates:
            return hold

        action, confidence, candidate, pattern, touch = max(candidates, key=lambda item: item[1])
        covered_points = candidate.covered_points
        first_point = covered_points[0]
        second_point = covered_points[1] if len(covered_points) > 1 else covered_points[0]
        third_point = covered_points[2] if len(covered_points) > 2 else covered_points[-1]

        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=min(confidence, 0.90),
            reason=(
                f"{pattern}:touches={len(covered_points)},"
                f"slope={candidate.normalized_slope:.4f}/bar,"
                f"mean_err={candidate.mean_error_atr:.3f}atr"
            ),
            used_indicators=used,
            metadata={
                "atr": atr,
                "pattern": pattern,
                "trendline_price": candidate.trendline_price_at_current,
                "touch_distance": touch.touch_distance,
                "slope_per_bar": candidate.slope_per_bar,
                "normalized_slope": candidate.normalized_slope,
                "mean_error_atr": candidate.mean_error_atr,
                "fit_error_atr": candidate.max_error_atr,
                "covered_point_count": len(covered_points),
                "covered_points": _covered_points_payload(covered_points),
                "anchor1_index": candidate.anchor1.index,
                "anchor1_price": candidate.anchor1.price,
                "anchor2_index": candidate.anchor2.index,
                "anchor2_price": candidate.anchor2.price,
                "p1_index": first_point.index,
                "p1_price": first_point.price,
                "p2_index": second_point.index,
                "p2_price": second_point.price,
                "p3_index": third_point.index,
                "p3_price": third_point.price,
                "span_bars": covered_points[-1].index - covered_points[0].index,
                "close": cur_close,
            },
        )

    def _compute_confidence(
        self,
        candidate: TrendlineCandidate,
        touch_distance: float,
        tolerance: float,
    ) -> float:
        conf = 0.48

        covered_count = len(candidate.covered_points)
        conf += min(max(covered_count - self._min_covered_points, 0) * 0.05, 0.15)

        span = candidate.covered_points[-1].index - candidate.covered_points[0].index
        if span > 10:
            conf += min((span - 10) / 50.0 * 0.10, 0.10)

        optimal = 0.06
        deviation = abs(candidate.normalized_slope - optimal) / optimal
        conf += max(0.08 * (1.0 - min(deviation, 1.0)), 0.0)

        mean_precision = max(1.0 - candidate.mean_error_atr / max(self._fit_tolerance_atr, 1e-6), 0.0)
        conf += mean_precision * 0.07

        max_precision = max(1.0 - candidate.max_error_atr / max(self._fit_tolerance_atr, 1e-6), 0.0)
        conf += max_precision * 0.07

        if tolerance > 0:
            conf += max(1.0 - touch_distance / tolerance, 0.0) * 0.08

        return min(conf, 0.90)
