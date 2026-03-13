from typing import Any, Dict, Iterable, List
import math

from .base import get_closes, get_float, get_int, tail_bars
from .mean import _ema_sequence


def atr(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=14, aliases=("window",))
    window = tail_bars(bars, period + 1)
    if len(window) <= period:
        return {}

    trs: List[float] = []
    prev_close = window[0].close
    for bar in window[1:]:
        tr = max(
            bar.high - bar.low,
            abs(bar.high - prev_close),
            abs(bar.low - prev_close),
        )
        trs.append(tr)
        prev_close = bar.close

    atr_val = sum(trs[-period:]) / period
    return {"atr": atr_val}


def bollinger(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=20, aliases=("window",))
    mult = get_float(params, "mult", default=2.0, aliases=("num_std",))
    closes = get_closes(bars, period)
    if len(closes) < period:
        return {}
    mean = sum(closes) / period
    var = sum((c - mean) ** 2 for c in closes) / period
    std = math.sqrt(var)
    upper = mean + mult * std
    lower = mean - mult * std
    return {"bb_mid": mean, "bb_upper": upper, "bb_lower": lower}


def keltner(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=20, aliases=("window",))
    atr_period = get_int(params, "atr_period", default=14)
    mult = get_float(params, "mult", default=2.0)
    window = tail_bars(bars, max(period, atr_period) + 1)
    if len(window) < max(period, atr_period):
        return {}

    typicals = [(b.high + b.low + b.close) / 3 for b in window]
    mid = _ema_sequence(typicals[-max(period * 3, period) :], period)

    # ATR 计算沿用现有逻辑
    trs: List[float] = []
    prev_close = window[0].close
    for bar in window[1:]:
        tr = max(
            bar.high - bar.low,
            abs(bar.high - prev_close),
            abs(bar.low - prev_close),
        )
        trs.append(tr)
        prev_close = bar.close
    atr_val = sum(trs[-atr_period:]) / atr_period if len(trs) >= atr_period else None
    if atr_val is None:
        return {}
    upper = mid + mult * atr_val
    lower = mid - mult * atr_val
    return {"kc_mid": mid, "kc_upper": upper, "kc_lower": lower}


def donchian(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=20, aliases=("window",))
    window = tail_bars(bars, period)
    if len(window) < period:
        return {}
    upper = max(bar.high for bar in window)
    lower = min(bar.low for bar in window)
    mid = (upper + lower) / 2
    return {"donchian_upper": upper, "donchian_lower": lower, "donchian_mid": mid}
