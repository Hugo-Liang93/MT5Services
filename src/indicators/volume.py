from typing import Any, Dict, Iterable, List

from .base import get_int, tail_bars


def obv(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=30, aliases=("window",))
    window = tail_bars(bars, period + 1)
    if len(window) < 2:
        return {}
    value = 0.0
    for prev, curr in zip(window[:-1], window[1:]):
        if curr.close > prev.close:
            value += curr.volume
        elif curr.close < prev.close:
            value -= curr.volume
    return {"obv": value}


def vwap(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=30, aliases=("window",))
    window = tail_bars(bars, period)
    if not window:
        return {}
    total_volume = sum(bar.volume for bar in window)
    if total_volume == 0:
        return {}
    sum_pv = sum(((bar.high + bar.low + bar.close) / 3) * bar.volume for bar in window)
    return {"vwap": sum_pv / total_volume}
