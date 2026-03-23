from typing import Any, Dict, Iterable

import numpy as np

from .base import get_hlcv_arrays, get_int, tail_bars


def obv(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=30, aliases=("window",))
    window = tail_bars(bars, period + 1)
    if len(window) < 2:
        return {}
    _, _, closes, volumes = get_hlcv_arrays(window)
    # 向量化：close 变化方向 × volume 累加
    price_diff = np.diff(closes)
    signs = np.sign(price_diff)
    value = float(np.dot(signs, volumes[1:]))
    return {"obv": value}


def vwap(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=30, aliases=("window",))
    window = tail_bars(bars, period)
    if not window:
        return {}
    highs, lows, closes, volumes = get_hlcv_arrays(window)
    total_volume = float(np.sum(volumes))
    if total_volume == 0:
        return {}
    # 向量化 typical price × volume
    typical_prices = (highs + lows + closes) / 3.0
    sum_pv = float(np.dot(typical_prices, volumes))
    return {"vwap": sum_pv / total_volume}


def mfi(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=14, aliases=("window",))
    window = tail_bars(bars, period + 1)
    if len(window) <= period:
        return {}
    highs, lows, closes, volumes = get_hlcv_arrays(window)
    typical_prices = (highs + lows + closes) / 3.0
    money_flow = typical_prices[1:] * volumes[1:]
    tp_diff = np.diff(typical_prices)
    positive_flow = float(np.sum(money_flow[tp_diff > 0]))
    negative_flow = float(np.sum(money_flow[tp_diff < 0]))
    if negative_flow == 0:
        return {"mfi": 100.0}
    money_ratio = positive_flow / negative_flow
    return {"mfi": 100.0 - (100.0 / (1.0 + money_ratio))}
