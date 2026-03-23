from typing import Any, Dict, Iterable, List

import numpy as np

from .base import get_closes_array, get_float, get_hlc_arrays, get_int, tail_bars
from .mean import _ema_sequence
from ..cache.incremental import IndicatorState, IncrementalIndicator


def _compute_tr_array(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray
) -> np.ndarray:
    """向量化计算 True Range 数组。

    输入 N 根 bar 的 HLC，返回 N-1 个 TR 值（从第 2 根 bar 开始）。
    """
    prev_closes = closes[:-1]
    h = highs[1:]
    l = lows[1:]
    return np.maximum(h - l, np.maximum(np.abs(h - prev_closes), np.abs(l - prev_closes)))


def atr(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=14, aliases=("window",))
    window = tail_bars(bars, period + 1)
    if len(window) <= period:
        return {}

    highs, lows, closes = get_hlc_arrays(window)
    trs = _compute_tr_array(highs, lows, closes)
    atr_val = float(np.mean(trs[-period:]))
    return {"atr": atr_val}


class AtrIncremental(IncrementalIndicator):
    """Incremental ATR using Wilder's smoothing: O(1) update per bar.

    Full computation seeds the state with a simple-average ATR over
    ``period`` bars.  Once seeded, each bar close only needs:

        new_tr  = max(H-L, |H-prev_close|, |L-prev_close|)
        new_atr = (prev_atr * (period - 1) + new_tr) / period

    ``prev_close`` is stored in ``state.intermediate_results`` so the
    next TR can be computed without any historical look-back.
    """

    def __init__(self, name: str, params: Dict[str, Any]) -> None:
        super().__init__(name, params)
        # ATR needs period + 1 bars to compute the first TR sequence
        self.min_data_points = get_int(params, "period", default=14, aliases=("window",)) + 1

    def _compute_full(self, bars: list) -> Dict[str, float]:
        return atr(bars, self.params)

    def _can_use_incremental(self, bars: list, state: IndicatorState) -> bool:
        if not super()._can_use_incremental(bars, state):
            return False
        return (
            state.intermediate_results is not None
            and "prev_close" in state.intermediate_results
        )

    def _compute_incremental(self, bars: list, state: IndicatorState) -> Dict[str, float]:
        period = get_int(self.params, "period", default=14, aliases=("window",))
        bar = bars[-1]
        prev_close = float(state.intermediate_results["prev_close"])  # type: ignore[index]
        tr = max(
            bar.high - bar.low,
            abs(bar.high - prev_close),
            abs(bar.low - prev_close),
        )
        prev_atr = float(state.value)
        return {"atr": (prev_atr * (period - 1) + tr) / period}

    def _create_new_state(self, bars: list, result: Dict[str, float]) -> IndicatorState:
        state = super()._create_new_state(bars, result)
        if bars:
            state.intermediate_results = {"prev_close": bars[-1].close}
        return state


def bollinger(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=20, aliases=("window",))
    mult = get_float(params, "mult", default=2.0, aliases=("num_std",))
    closes = get_closes_array(bars, period)
    if len(closes) < period:
        return {}
    # numpy 向量化：ddof=0 对应总体标准差（与原实现一致）
    mean = float(np.mean(closes))
    std = float(np.std(closes))
    upper = mean + mult * std
    lower = mean - mult * std
    # close 字段：策略比较 close vs 带宽时需要当前收盘价，
    # 由指标本身携带，避免策略层多级回退猜测
    return {"bb_mid": mean, "bb_upper": upper, "bb_lower": lower, "close": float(closes[-1])}


class BollingerIncremental(IncrementalIndicator):
    """Incremental Bollinger Bands: O(1) update using running sums.

    Full computation seeds the state with ``running_sum``,
    ``running_sq_sum`` and a ring buffer of the last ``period`` closes.
    Once seeded, each new bar only needs:

        running_sum  = running_sum  - dropped + new_close
        running_sq_sum = running_sq_sum - dropped² + new_close²
        mean = running_sum / period
        std  = sqrt(running_sq_sum / period - mean²)
    """

    def __init__(self, name: str, params: Dict[str, Any]) -> None:
        super().__init__(name, params)
        self.min_data_points = get_int(params, "period", default=20, aliases=("window",))

    def _compute_full(self, bars: list) -> Dict[str, float]:
        return bollinger(bars, self.params)

    def _can_use_incremental(self, bars: list, state: IndicatorState) -> bool:
        if not super()._can_use_incremental(bars, state):
            return False
        ir = state.intermediate_results
        return (
            ir is not None
            and "running_sum" in ir
            and "running_sq_sum" in ir
            and "last_closes" in ir
        )

    def _compute_incremental(self, bars: list, state: IndicatorState) -> Dict[str, float]:
        period = get_int(self.params, "period", default=20, aliases=("window",))
        mult = get_float(self.params, "mult", default=2.0, aliases=("num_std",))
        running_sum = float(state.intermediate_results["running_sum"])  # type: ignore[index]
        running_sq_sum = float(state.intermediate_results["running_sq_sum"])  # type: ignore[index]
        last_closes = list(state.intermediate_results["last_closes"])  # type: ignore[index]
        new_close = bars[-1].close
        dropped = last_closes[0] if len(last_closes) >= period else 0.0
        running_sum = running_sum - dropped + new_close
        running_sq_sum = running_sq_sum - dropped ** 2 + new_close ** 2
        mean = running_sum / period
        var = running_sq_sum / period - mean ** 2
        std = var ** 0.5 if var > 0 else 0.0
        return {
            "bb_mid": mean,
            "bb_upper": mean + mult * std,
            "bb_lower": mean - mult * std,
            "close": new_close,
        }

    def _create_new_state(self, bars: list, result: Dict[str, float]) -> IndicatorState:
        state = super()._create_new_state(bars, result)
        period = get_int(self.params, "period", default=20, aliases=("window",))
        closes = get_closes_array(bars, period)
        state.intermediate_results = {
            "running_sum": float(np.sum(closes)),
            "running_sq_sum": float(np.sum(closes ** 2)),
            "last_closes": closes.tolist(),
        }
        return state


def keltner(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=20, aliases=("window",))
    atr_period = get_int(params, "atr_period", default=14)
    mult = get_float(params, "mult", default=2.0)
    window = tail_bars(bars, max(period, atr_period) + 1)
    if len(window) < max(period, atr_period):
        return {}

    highs, lows, closes = get_hlc_arrays(window)
    # typical price 向量化
    typicals = ((highs + lows + closes) / 3.0).tolist()
    mid = _ema_sequence(typicals[-max(period * 3, period):], period)

    # TR 向量化
    trs = _compute_tr_array(highs, lows, closes)
    if len(trs) < atr_period:
        return {}
    atr_val = float(np.mean(trs[-atr_period:]))
    upper = mid + mult * atr_val
    lower = mid - mult * atr_val
    return {"kc_mid": mid, "kc_upper": upper, "kc_lower": lower}


def donchian(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=20, aliases=("window",))
    window = tail_bars(bars, period)
    if len(window) < period:
        return {}
    highs, lows, closes = get_hlc_arrays(window)
    upper = float(np.max(highs))
    lower = float(np.min(lows))
    mid = (upper + lower) / 2
    # close 字段：DonchianBreakoutStrategy 比较 close vs 通道边界时使用
    return {"donchian_upper": upper, "donchian_lower": lower, "donchian_mid": mid, "close": float(closes[-1])}


def adx(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=14, aliases=("window",))
    window = tail_bars(bars, period * 3)
    if len(window) < (period * 2) + 1:
        return {}

    highs, lows, closes = get_hlc_arrays(window)

    # 向量化提取 DM 和 TR
    up_move = np.diff(highs)   # curr.high - prev.high
    down_move = -np.diff(lows)  # prev.low - curr.low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    trs = _compute_tr_array(highs, lows, closes)

    n = len(trs)
    if n < period * 2:
        return {}

    # Wilder 平滑（有序列依赖，保留循环）
    atr_val = float(np.sum(trs[:period]))
    smooth_plus = float(np.sum(plus_dm[:period]))
    smooth_minus = float(np.sum(minus_dm[:period]))
    dx_values: List[float] = []
    plus_di = 0.0
    minus_di = 0.0

    for idx in range(period, n):
        atr_val = atr_val - (atr_val / period) + float(trs[idx])
        smooth_plus = smooth_plus - (smooth_plus / period) + float(plus_dm[idx])
        smooth_minus = smooth_minus - (smooth_minus / period) + float(minus_dm[idx])
        if atr_val <= 0:
            continue
        plus_di = 100.0 * (smooth_plus / atr_val)
        minus_di = 100.0 * (smooth_minus / atr_val)
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
