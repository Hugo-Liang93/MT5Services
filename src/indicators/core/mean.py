from typing import Dict, Any, Iterable, List

import numpy as np

from .base import get_closes, get_closes_array, get_int
from ..cache.incremental import IndicatorState, IncrementalIndicator


def _ema_sequence(values: List[float], period: int) -> float:
    k = 2 / (period + 1)
    ema_val = values[0]
    for price in values[1:]:
        ema_val = ema_val + k * (price - ema_val)
    return ema_val


def sma(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=20, aliases=("window",))
    closes = get_closes_array(bars, period)
    if len(closes) < period:
        return {}
    return {"sma": float(np.mean(closes))}


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
    closes = get_closes_array(bars, period)
    if len(closes) < period:
        return {}
    weights = np.arange(1, period + 1, dtype=np.float64)
    total_w = float(np.sum(weights))
    value = float(np.dot(closes[-period:], weights)) / total_w
    return {"wma": value}


def _wma_raw(values: List[float], period: int) -> float:
    """对已有浮点序列计算 WMA，不依赖 OHLC 对象。"""
    window = values[-period:]
    n = len(window)
    if n == 0:
        return 0.0
    arr = np.array(window, dtype=np.float64)
    weights = np.arange(1, n + 1, dtype=np.float64)
    total_w = float(np.sum(weights))
    return float(np.dot(arr, weights)) / total_w if total_w else 0.0


def hma(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    """Hull Moving Average — 大幅减少滞后的移动均线。

    算法：HMA(n) = WMA(sqrt(n), 2 × WMA(n/2) - WMA(n))

    优化：只需最后 sqrt_period 个 hull 差值即可做最终 WMA，
    无需遍历全部历史位置。

    输出：
        hma  — Hull 移动平均值
    """
    import math as _math

    period = get_int(params, "period", default=20, aliases=("window",))
    half_period = max(period // 2, 1)
    sqrt_period = max(int(_math.sqrt(period)), 1)

    # 只需 period + sqrt_period - 1 个 close 即可产生 sqrt_period 个 hull 差值
    needed = period + sqrt_period - 1
    closes = get_closes(bars, needed + 5)
    if len(closes) < needed:
        return {}

    # 只生成最后 sqrt_period 个 hull 差值（足够做 WMA(sqrt_period)）
    hull_series: List[float] = []
    end_len = len(closes)
    first_pos = end_len - sqrt_period
    for pos in range(first_pos, end_len):
        window_end = pos + 1
        full_wma = _wma_raw(closes[:window_end], period)
        half_wma = _wma_raw(closes[:window_end], half_period)
        hull_series.append(2.0 * half_wma - full_wma)

    if len(hull_series) < sqrt_period:
        return {}

    hma_val = _wma_raw(hull_series, sqrt_period)
    return {"hma": hma_val}
