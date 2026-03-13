from typing import Dict, Any, Iterable, List

from .base import get_closes, get_int


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


def wma(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=20, aliases=("window",))
    closes = get_closes(bars, period)
    if len(closes) < period:
        return {}
    weights = list(range(1, period + 1))
    total_w = sum(weights)
    value = sum(c * w for c, w in zip(closes[-period:], weights)) / total_w
    return {"wma": value}
