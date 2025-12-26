from typing import Any, Dict, Iterable, List

from .base import get_closes, get_int
from .mean import _ema_sequence


def rsi(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=14, aliases=("window",))
    closes = get_closes(bars, period + 1)
    if len(closes) <= period:
        return {}
    gains: List[float] = []
    losses: List[float] = []
    for prev, curr in zip(closes[:-1], closes[1:]):
        diff = curr - prev
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-diff)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return {"rsi": 100.0}
    rs = avg_gain / avg_loss
    rsi_val = 100 - (100 / (1 + rs))
    return {"rsi": rsi_val}


def macd(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    fast = get_int(params, "fast", default=12)
    slow = get_int(params, "slow", default=26)
    signal = get_int(params, "signal", default=9)
    closes = get_closes(bars, slow + signal + 5)
    if len(closes) < slow + signal:
        return {}

    k_fast = 2 / (fast + 1)
    k_slow = 2 / (slow + 1)
    ema_fast = None
    ema_slow = None
    macd_series: List[float] = []
    for price in closes:
        ema_fast = price if ema_fast is None else ema_fast + k_fast * (price - ema_fast)
        ema_slow = price if ema_slow is None else ema_slow + k_slow * (price - ema_slow)
        macd_series.append(ema_fast - ema_slow)

    if len(macd_series) < signal:
        return {}

    signal_val = _ema_sequence(macd_series[-(signal * 3) :], signal)
    macd_val = macd_series[-1]
    hist_val = macd_val - signal_val
    return {"macd": macd_val, "signal": signal_val, "hist": hist_val}


def roc(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=12, aliases=("window",))
    closes = get_closes(bars, period + 1)
    if len(closes) <= period:
        return {}
    prev = closes[-(period + 1)]
    last = closes[-1]
    if prev == 0:
        return {}
    value = (last - prev) / prev * 100
    return {"roc": value}


def cci(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=20, aliases=("window",))
    window = list(bars)[-period:] if not isinstance(bars, list) else bars[-period:]
    if len(window) < period:
        return {}
    typicals = [(b.high + b.low + b.close) / 3 for b in window]
    mean_tp = sum(typicals) / period
    mean_dev = sum(abs(tp - mean_tp) for tp in typicals) / period
    if mean_dev == 0:
        return {}
    last_tp = typicals[-1]
    cci_val = (last_tp - mean_tp) / (0.015 * mean_dev)
    return {"cci": cci_val}
