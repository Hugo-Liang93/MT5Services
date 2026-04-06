"""趋势线检测纯函数工具库。

提供 swing 检测、趋势线拟合、触碰评估等纯函数，
供 StructuredTrendlineTouch 策略使用。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Sequence


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
