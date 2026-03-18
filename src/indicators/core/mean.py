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
