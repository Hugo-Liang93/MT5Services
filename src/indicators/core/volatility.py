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


def adx(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=14, aliases=("window",))
    window = tail_bars(bars, period * 3)
    if len(window) < (period * 2) + 1:
        return {}

    trs: List[float] = []
    plus_dm: List[float] = []
    minus_dm: List[float] = []
    for prev, curr in zip(window[:-1], window[1:]):
        up_move = curr.high - prev.high
        down_move = prev.low - curr.low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        trs.append(
            max(
                curr.high - curr.low,
                abs(curr.high - prev.close),
                abs(curr.low - prev.close),
            )
        )

    if len(trs) < period * 2:
        return {}

    atr = sum(trs[:period])
    smooth_plus_dm = sum(plus_dm[:period])
    smooth_minus_dm = sum(minus_dm[:period])
    dx_values: List[float] = []
    plus_di = 0.0
    minus_di = 0.0

    for idx in range(period, len(trs)):
        atr = atr - (atr / period) + trs[idx]
        smooth_plus_dm = smooth_plus_dm - (smooth_plus_dm / period) + plus_dm[idx]
        smooth_minus_dm = smooth_minus_dm - (smooth_minus_dm / period) + minus_dm[idx]
        if atr <= 0:
            continue
        plus_di = 100.0 * (smooth_plus_dm / atr)
        minus_di = 100.0 * (smooth_minus_dm / atr)
        di_sum = plus_di + minus_di
        if di_sum == 0:
            dx_values.append(0.0)
        else:
            dx_values.append(abs(plus_di - minus_di) / di_sum * 100.0)

    if len(dx_values) < period:
        return {}
    adx_value = sum(dx_values[-period:]) / period
    return {
        "adx": adx_value,
        "plus_di": plus_di,
        "minus_di": minus_di,
    }
