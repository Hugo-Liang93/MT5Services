from typing import Any, Dict, Iterable, List

from .base import get_int, tail_bars


def obv(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=30, aliases=("window",))
    window = tail_bars(bars, period + 1)
    if len(window) < 2:
        return {}
    value = 0.0
    for prev, curr in zip(window[:-1], window[1:]):
        vol = float(curr.volume or 0.0)  # 修复：MT5某些品种volume可能为None
        if curr.close > prev.close:
            value += vol
        elif curr.close < prev.close:
            value -= vol
    return {"obv": value}


def vwap(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=30, aliases=("window",))
    window = tail_bars(bars, period)
    if not window:
        return {}
    total_volume = sum(float(bar.volume or 0.0) for bar in window)  # 修复：volume可能为None
    if total_volume == 0:
        return {}
    sum_pv = sum(((bar.high + bar.low + bar.close) / 3) * float(bar.volume or 0.0) for bar in window)
    return {"vwap": sum_pv / total_volume}


def mfi(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=14, aliases=("window",))
    window = tail_bars(bars, period + 1)
    if len(window) <= period:
        return {}
    positive_flow = 0.0
    negative_flow = 0.0
    previous_typical = (window[0].high + window[0].low + window[0].close) / 3.0
    for bar in window[1:]:
        typical_price = (bar.high + bar.low + bar.close) / 3.0
        money_flow = typical_price * float(bar.volume or 0.0)
        if typical_price > previous_typical:
            positive_flow += money_flow
        elif typical_price < previous_typical:
            negative_flow += money_flow
        previous_typical = typical_price
    if negative_flow == 0:
        return {"mfi": 100.0}
    money_ratio = positive_flow / negative_flow
    return {"mfi": 100.0 - (100.0 / (1.0 + money_ratio))}
