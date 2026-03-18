from typing import Dict, Any, Iterable, List

from .base import get_closes, get_int
from ..cache.incremental import IndicatorState, IncrementalIndicator


def _ema_sequence(values: List[float], period: int) -> float:
    k = 2 / (period + 1)
    ema_val = values[0]
    for price in values[1:]:
        ema_val = ema_val + k * (price - ema_val)
    return ema_val


def sma(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=20, aliases=("window",))
    closes = get_closes(bars, period)
    if len(closes) < period:
        return {}
    return {"sma": sum(closes) / period}


def ema(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=20, aliases=("window",))
    # 预留多一点历史以平滑初始 EMA
    closes = get_closes(bars, max(period * 3, period))
    if len(closes) < period:
        return {}
    value = _ema_sequence(closes, period)
    return {"ema": value}


class EmaIncremental(IncrementalIndicator):
    """Incremental EMA: O(1) update using saved previous EMA value.

    Full computation seeds the state with ``_ema_sequence`` over
    ``period * 3`` bars.  Once seeded, each bar close only needs:

        new_ema = prev_ema + k * (new_close - prev_ema)
    """

    def __init__(self, name: str, params: Dict[str, Any]) -> None:
        super().__init__(name, params)
        self.min_data_points = get_int(params, "period", default=20, aliases=("window",))

    def _compute_full(self, bars: list) -> Dict[str, float]:
        return ema(bars, self.params)

    def _compute_incremental(self, bars: list, state: IndicatorState) -> Dict[str, float]:
        period = get_int(self.params, "period", default=20, aliases=("window",))
        k = 2.0 / (period + 1)
        prev_ema = float(state.value)
        new_close = bars[-1].close
        return {"ema": prev_ema + k * (new_close - prev_ema)}


def wma(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=20, aliases=("window",))
    closes = get_closes(bars, period)
    if len(closes) < period:
        return {}
    weights = list(range(1, period + 1))
    total_w = sum(weights)
    value = sum(c * w for c, w in zip(closes[-period:], weights)) / total_w
    return {"wma": value}


def _wma_raw(values: List[float], period: int) -> float:
    """对已有浮点序列计算 WMA，不依赖 OHLC 对象。"""
    window = values[-period:]
    weights = list(range(1, len(window) + 1))
    total_w = sum(weights)
    return sum(v * w for v, w in zip(window, weights)) / total_w if total_w else 0.0


def hma(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    """Hull Moving Average — 大幅减少滞后的移动均线。

    算法：HMA(n) = WMA(sqrt(n), 2 × WMA(n/2) - WMA(n))

    HMA 的响应速度是普通 WMA 的 2 倍，同时保持价格平滑性，
    非常适合黄金日内趋势判断。

    输出：
        hma  — Hull 移动平均值
    """
    import math as _math

    period = get_int(params, "period", default=20, aliases=("window",))
    half_period = max(period // 2, 1)
    sqrt_period = max(int(_math.sqrt(period)), 1)

    # 计算 HMA 需要足够数据：完整 WMA(n) 和 WMA(n/2) 后还要对差值再做 WMA(sqrt(n))
    # 总需求 ≈ period + sqrt_period
    needed = period + sqrt_period + 5
    closes = get_closes(bars, needed)
    if len(closes) < period + sqrt_period:
        return {}

    # 对每个位置计算 2×WMA(n/2) - WMA(n)，构成 hull_series
    hull_series: List[float] = []
    start_idx = period - 1  # WMA(n) 需要 period 个点，从 period-1 开始可用
    for i in range(start_idx, len(closes)):
        full_wma = _wma_raw(closes[: i + 1], period)
        half_wma = _wma_raw(closes[: i + 1], half_period)
        hull_series.append(2.0 * half_wma - full_wma)

    if len(hull_series) < sqrt_period:
        return {}

    hma_val = _wma_raw(hull_series, sqrt_period)
    return {"hma": hma_val}
