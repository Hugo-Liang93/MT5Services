"""三点确认趋势线策略

连接 3 个依次抬高的 swing low（上升趋势线）或 3 个依次降低的 swing high
（下降趋势线），价格第 3 次触碰趋势线时顺势入场。

仅在 H1/H4 级别运行（低 TF swing 点不稳定，趋势线不可靠）。
斜率以 ATR 归一化，过滤太平（横盘）和太陡（不可持续）的线。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..evaluation.regime import RegimeType
from ..models import SignalContext, SignalDecision
from .base import _resolve_indicator_value, get_tf_param

logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SwingPoint:
    """检测到的 swing high/low 极值点。"""

    index: int  # 在 recent_bars 数组中的位置
    price: float  # high（swing high）或 low（swing low）


@dataclass(frozen=True)
class TrendlineResult:
    """经过三点验证的趋势线。"""

    direction: str  # "ascending" or "descending"
    point1: SwingPoint
    point2: SwingPoint
    point3: SwingPoint
    slope_per_bar: float  # 每 bar 的原始斜率
    normalized_slope: float  # 斜率 / ATR
    fit_error: float  # 第 3 点偏离程度（ATR 归一化）
    trendline_price_at_current: float  # 趋势线在当前 bar 的投射价格


# ── Bar 提取工具 ─────────────────────────────────────────────────────


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


# ── Swing 检测 ───────────────────────────────────────────────────────


def _detect_swing_lows(
    bars: List[Any],
    window: int,
    min_spacing: int,
) -> List[SwingPoint]:
    """检测 swing low：bar[i].low 是前后各 window 根 bar 中的最低点。

    min_spacing 约束两个 swing 之间的最小 bar 间距（冲突时保留更低的）。
    """
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
    """检测 swing high：bar[i].high 是前后各 window 根 bar 中的最高点。"""
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


# ── 趋势线拟合 ───────────────────────────────────────────────────────


def _find_ascending_trendline(
    swing_lows: List[SwingPoint],
    atr: float,
    current_bar_index: int,
    *,
    min_slope_atr: float = 0.01,
    max_slope_atr: float = 0.15,
    fit_tolerance_atr: float = 0.5,
) -> Optional[TrendlineResult]:
    """从 3 个依次抬高的 swing low 中找上升趋势线。

    从最近的 swing 向前搜索，首个满足条件的三元组即返回（最近 = 最可操作）。
    """
    if len(swing_lows) < 3:
        return None

    for i in range(len(swing_lows) - 1, 1, -1):
        p3 = swing_lows[i]
        for j in range(i - 1, 0, -1):
            p2 = swing_lows[j]
            if p3.price <= p2.price:
                continue
            for k in range(j - 1, -1, -1):
                p1 = swing_lows[k]
                if p2.price <= p1.price:
                    continue

                dx = p2.index - p1.index
                if dx == 0:
                    continue
                slope = (p2.price - p1.price) / dx
                norm_slope = slope / atr if atr > 0 else 0.0
                if norm_slope < min_slope_atr or norm_slope > max_slope_atr:
                    continue

                predicted_p3 = p1.price + slope * (p3.index - p1.index)
                fit_error = abs(p3.price - predicted_p3)
                if fit_error > fit_tolerance_atr * atr:
                    continue

                tl_at_current = p1.price + slope * (current_bar_index - p1.index)
                return TrendlineResult(
                    direction="ascending",
                    point1=p1,
                    point2=p2,
                    point3=p3,
                    slope_per_bar=slope,
                    normalized_slope=norm_slope,
                    fit_error=fit_error / atr if atr > 0 else 0.0,
                    trendline_price_at_current=tl_at_current,
                )
    return None


def _find_descending_trendline(
    swing_highs: List[SwingPoint],
    atr: float,
    current_bar_index: int,
    *,
    min_slope_atr: float = 0.01,
    max_slope_atr: float = 0.15,
    fit_tolerance_atr: float = 0.5,
) -> Optional[TrendlineResult]:
    """从 3 个依次降低的 swing high 中找下降趋势线。"""
    if len(swing_highs) < 3:
        return None

    for i in range(len(swing_highs) - 1, 1, -1):
        p3 = swing_highs[i]
        for j in range(i - 1, 0, -1):
            p2 = swing_highs[j]
            if p3.price >= p2.price:
                continue
            for k in range(j - 1, -1, -1):
                p1 = swing_highs[k]
                if p2.price >= p1.price:
                    continue

                dx = p2.index - p1.index
                if dx == 0:
                    continue
                slope = (p2.price - p1.price) / dx  # 负数
                norm_slope = abs(slope) / atr if atr > 0 else 0.0
                if norm_slope < min_slope_atr or norm_slope > max_slope_atr:
                    continue

                predicted_p3 = p1.price + slope * (p3.index - p1.index)
                fit_error = abs(p3.price - predicted_p3)
                if fit_error > fit_tolerance_atr * atr:
                    continue

                tl_at_current = p1.price + slope * (current_bar_index - p1.index)
                return TrendlineResult(
                    direction="descending",
                    point1=p1,
                    point2=p2,
                    point3=p3,
                    slope_per_bar=slope,
                    normalized_slope=norm_slope,
                    fit_error=fit_error / atr if atr > 0 else 0.0,
                    trendline_price_at_current=tl_at_current,
                )
    return None


# ── 策略类 ───────────────────────────────────────────────────────────


class TrendlineThreeTouchStrategy:
    """三点确认趋势线入场策略。

    连接 3 个 swing low（上升）或 swing high（下降）形成趋势线，
    当前 bar 触碰趋势线时顺势入场。

    仅 H1/H4 级别运行，低 TF 噪声导致趋势线不可靠。
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

        ascending = _find_ascending_trendline(
            swing_lows, atr, current_idx,
            min_slope_atr=min_slope, max_slope_atr=max_slope, fit_tolerance_atr=fit_tol,
        )
        descending = _find_descending_trendline(
            swing_highs, atr, current_idx,
            min_slope_atr=min_slope, max_slope_atr=max_slope, fit_tolerance_atr=fit_tol,
        )

        candidates: List[tuple[str, float, TrendlineResult, str]] = []

        if ascending is not None:
            tl_price = ascending.trendline_price_at_current
            tolerance = touch_tol * atr
            if cur_low <= tl_price + tolerance and cur_close > tl_price:
                conf = self._compute_confidence(ascending, atr, cur_low, tl_price, tolerance)
                candidates.append(("buy", conf, ascending, "ascending_trendline_touch"))

        if descending is not None:
            tl_price = descending.trendline_price_at_current
            tolerance = touch_tol * atr
            if cur_high >= tl_price - tolerance and cur_close < tl_price:
                conf = self._compute_confidence(descending, atr, cur_high, tl_price, tolerance)
                candidates.append(("sell", conf, descending, "descending_trendline_touch"))

        if not candidates:
            return hold

        action, confidence, tl, pattern = max(candidates, key=lambda x: x[1])
        return SignalDecision(
            strategy=self.name,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=action,
            confidence=min(confidence, 0.90),
            reason=(
                f"{pattern}:slope={tl.normalized_slope:.4f}/bar,"
                f"span={tl.point3.index - tl.point1.index}bars,"
                f"fit={tl.fit_error:.3f}atr"
            ),
            used_indicators=used,
            metadata={
                "atr": atr,
                "pattern": pattern,
                "trendline_price": tl.trendline_price_at_current,
                "slope_per_bar": tl.slope_per_bar,
                "normalized_slope": tl.normalized_slope,
                "fit_error_atr": tl.fit_error,
                "p1_index": tl.point1.index,
                "p1_price": tl.point1.price,
                "p2_index": tl.point2.index,
                "p2_price": tl.point2.price,
                "p3_index": tl.point3.index,
                "p3_price": tl.point3.price,
                "span_bars": tl.point3.index - tl.point1.index,
                "close": cur_close,
            },
        )

    def _compute_confidence(
        self,
        tl: TrendlineResult,
        atr: float,
        touch_price: float,
        tl_price: float,
        tolerance: float,
    ) -> float:
        conf = 0.50

        # 趋势线跨度 bonus（10~60+ bar）
        span = tl.point3.index - tl.point1.index
        if span > 10:
            conf += min((span - 10) / 50.0 * 0.12, 0.12)

        # 斜率质量 bonus（0.06 ATR/bar 附近最优）
        optimal = 0.06
        deviation = abs(tl.normalized_slope - optimal) / optimal
        conf += max(0.10 * (1.0 - min(deviation, 1.0)), 0.0)

        # 第 3 点拟合精度 bonus
        fit_precision = max(1.0 - tl.fit_error / self._fit_tolerance_atr, 0.0)
        conf += fit_precision * 0.10

        # 触碰精度 bonus
        if tolerance > 0:
            touch_dist = abs(touch_price - tl_price)
            conf += max(1.0 - touch_dist / tolerance, 0.0) * 0.08

        return min(conf, 0.90)
