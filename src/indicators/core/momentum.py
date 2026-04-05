from typing import Any, Dict, Iterable, List

import numpy as np

from .base import get_closes, get_closes_array, get_float, get_hlc_arrays, get_int, tail_bars
from .mean import _ema_sequence
from ..cache.incremental import IndicatorState, IncrementalIndicator


def rsi(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    """RSI — 使用 Wilder 平滑（标准实现）。

    Bug修复：原实现仅用最后 period+1 根 bar 做简单平均初始化，
    导致初始阶段 RSI 偏差较大。改为用更多历史数据（period*3）做
    Wilder 指数平滑，与主流交易软件（MT5、TradingView）结果一致。
    """
    period = get_int(params, "period", default=14, aliases=("window",))
    # 使用更多历史数据让 Wilder 平滑收敛
    closes = get_closes_array(bars, period * 3 + 1)
    if len(closes) <= period:
        return {}

    # 向量化 diff + gains/losses 提取
    diffs = np.diff(closes)
    gains = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)

    # 首 period 根用简单平均初始化（Wilder 标准做法）
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))

    # Wilder 平滑余下的数据点（有序列依赖，保留循环）
    for i in range(period, len(diffs)):
        avg_gain = (avg_gain * (period - 1) + float(gains[i])) / period
        avg_loss = (avg_loss * (period - 1) + float(losses[i])) / period

    if avg_loss == 0:
        return {"rsi": 100.0}
    rs = avg_gain / avg_loss
    rsi_val = 100.0 - (100.0 / (1.0 + rs))
    return {"rsi": rsi_val}


class RsiIncremental(IncrementalIndicator):
    """Incremental RSI using Wilder smoothing: O(1) update per bar.

    Full computation seeds the state with ``avg_gain``, ``avg_loss`` and
    ``prev_close`` using the full Wilder smoothing pass.  Once seeded,
    each new bar only needs:

        diff = new_close - prev_close
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    """

    def __init__(self, name: str, params: Dict[str, Any]) -> None:
        super().__init__(name, params)
        self.min_data_points = get_int(params, "period", default=14, aliases=("window",)) + 1

    def _compute_full(self, bars: list) -> Dict[str, float]:
        return rsi(bars, self.params)

    def _can_use_incremental(self, bars: list, state: IndicatorState) -> bool:
        if not super()._can_use_incremental(bars, state):
            return False
        ir = state.intermediate_results
        return (
            ir is not None
            and "avg_gain" in ir
            and "avg_loss" in ir
            and "prev_close" in ir
        )

    def _compute_incremental(self, bars: list, state: IndicatorState) -> Dict[str, float]:
        period = get_int(self.params, "period", default=14, aliases=("window",))
        avg_gain = float(state.intermediate_results["avg_gain"])  # type: ignore[index]
        avg_loss = float(state.intermediate_results["avg_loss"])  # type: ignore[index]
        prev_close = float(state.intermediate_results["prev_close"])  # type: ignore[index]
        new_close = bars[-1].close
        diff = new_close - prev_close
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            return {"rsi": 100.0}
        rs = avg_gain / avg_loss
        return {"rsi": 100.0 - (100.0 / (1.0 + rs))}

    def _create_new_state(self, bars: list, result: Dict[str, float]) -> IndicatorState:
        state = super()._create_new_state(bars, result)
        period = get_int(self.params, "period", default=14, aliases=("window",))
        # Recompute avg_gain/avg_loss from full bar history for accurate state
        closes = get_closes_array(bars, period * 3 + 1)
        if len(closes) > period:
            diffs = np.diff(closes)
            gains = np.where(diffs > 0, diffs, 0.0)
            losses = np.where(diffs < 0, -diffs, 0.0)
            avg_gain = float(np.mean(gains[:period]))
            avg_loss = float(np.mean(losses[:period]))
            for i in range(period, len(diffs)):
                avg_gain = (avg_gain * (period - 1) + float(gains[i])) / period
                avg_loss = (avg_loss * (period - 1) + float(losses[i])) / period
            state.intermediate_results = {
                "avg_gain": avg_gain,
                "avg_loss": avg_loss,
                "prev_close": bars[-1].close,
            }
        return state


def macd(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    fast = get_int(params, "fast", default=12)
    slow = get_int(params, "slow", default=26)
    signal = get_int(params, "signal", default=9)
    closes = get_closes(bars, slow + signal + 5)
    if len(closes) < slow + signal:
        return {}

    # EMA 有序列依赖，保留循环
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

    signal_val = _ema_sequence(macd_series[-(signal * 3):], signal)
    macd_val = macd_series[-1]
    hist_val = macd_val - signal_val
    return {"macd": macd_val, "signal": signal_val, "hist": hist_val}


class MacdIncremental(IncrementalIndicator):
    """Incremental MACD: O(1) update using three EMA states.

    State: ema_fast, ema_slow, ema_signal (three scalar values).
    Each bar: one EMA step per state.
    """

    def __init__(self, name: str, params: Dict[str, Any]) -> None:
        super().__init__(name, params)
        fast = get_int(params, "fast", default=12)
        slow = get_int(params, "slow", default=26)
        signal = get_int(params, "signal", default=9)
        self.min_data_points = slow + signal

    def _compute_full(self, bars: list) -> Dict[str, float]:
        return macd(bars, self.params)

    def _can_use_incremental(self, bars: list, state: IndicatorState) -> bool:
        if not super()._can_use_incremental(bars, state):
            return False
        ir = state.intermediate_results
        return (
            ir is not None
            and "ema_fast" in ir
            and "ema_slow" in ir
            and "ema_signal" in ir
        )

    def _compute_incremental(self, bars: list, state: IndicatorState) -> Dict[str, float]:
        fast = get_int(self.params, "fast", default=12)
        slow = get_int(self.params, "slow", default=26)
        signal = get_int(self.params, "signal", default=9)
        k_fast = 2.0 / (fast + 1)
        k_slow = 2.0 / (slow + 1)
        k_signal = 2.0 / (signal + 1)

        ir = state.intermediate_results
        ema_fast = float(ir["ema_fast"])  # type: ignore[index]
        ema_slow = float(ir["ema_slow"])  # type: ignore[index]
        ema_signal_val = float(ir["ema_signal"])  # type: ignore[index]

        new_close = bars[-1].close
        ema_fast = ema_fast + k_fast * (new_close - ema_fast)
        ema_slow = ema_slow + k_slow * (new_close - ema_slow)
        macd_val = ema_fast - ema_slow
        ema_signal_val = ema_signal_val + k_signal * (macd_val - ema_signal_val)
        hist_val = macd_val - ema_signal_val

        return {"macd": macd_val, "signal": ema_signal_val, "hist": hist_val}

    def _create_new_state(self, bars: list, result: Dict[str, float]) -> IndicatorState:
        state = super()._create_new_state(bars, result)
        fast = get_int(self.params, "fast", default=12)
        slow = get_int(self.params, "slow", default=26)
        signal = get_int(self.params, "signal", default=9)

        from .base import get_closes
        closes = get_closes(bars, slow + signal + 5)
        if len(closes) < slow + signal:
            return state

        k_fast = 2.0 / (fast + 1)
        k_slow = 2.0 / (slow + 1)
        ema_f: float | None = None
        ema_s: float | None = None
        macd_series: List[float] = []
        for price in closes:
            ema_f = price if ema_f is None else ema_f + k_fast * (price - ema_f)
            ema_s = price if ema_s is None else ema_s + k_slow * (price - ema_s)
            macd_series.append(ema_f - ema_s)

        ema_signal_val = _ema_sequence(macd_series[-(signal * 3):], signal)

        state.intermediate_results = {
            "ema_fast": ema_f,
            "ema_slow": ema_s,
            "ema_signal": ema_signal_val,
        }
        return state


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
    window = tail_bars(bars, period)
    if len(window) < period:
        return {}
    highs, lows, closes = get_hlc_arrays(window)
    # 向量化 typical price 和 mean deviation
    typicals = (highs + lows + closes) / 3.0
    mean_tp = float(np.mean(typicals))
    mean_dev = float(np.mean(np.abs(typicals - mean_tp)))
    if mean_dev == 0:
        return {}
    last_tp = float(typicals[-1])
    cci_val = (last_tp - mean_tp) / (0.015 * mean_dev)
    return {"cci": cci_val}


def stochastic(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    k_period = get_int(params, "k_period", default=14, aliases=("period",))
    d_period = get_int(params, "d_period", default=3)
    window = tail_bars(bars, k_period + d_period - 1)
    if len(window) < k_period:
        return {}

    highs, lows, closes = get_hlc_arrays(window)

    # 滑动窗口计算 %K
    k_values: List[float] = []
    for end_idx in range(k_period, len(window) + 1):
        h_slice = highs[end_idx - k_period:end_idx]
        l_slice = lows[end_idx - k_period:end_idx]
        highest_high = float(np.max(h_slice))
        lowest_low = float(np.min(l_slice))
        if highest_high == lowest_low:
            k_values.append(50.0)
            continue
        k_values.append((float(closes[end_idx - 1]) - lowest_low) / (highest_high - lowest_low) * 100.0)

    if not k_values:
        return {}
    k_value = k_values[-1]
    d_window = k_values[-d_period:] if len(k_values) >= d_period else k_values
    d_value = sum(d_window) / len(d_window)
    return {"stoch_k": k_value, "stoch_d": d_value}


def williams_r(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    period = get_int(params, "period", default=14, aliases=("lookback",))
    window = tail_bars(bars, period)
    if len(window) < period:
        return {}
    highs, lows, closes = get_hlc_arrays(window)
    highest_high = float(np.max(highs))
    lowest_low = float(np.min(lows))
    if highest_high == lowest_low:
        return {"williams_r": 0.0}
    value = (highest_high - float(closes[-1])) / (highest_high - lowest_low) * -100.0
    return {"williams_r": value}


def supertrend(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    """Supertrend 趋势跟踪指标，基于 ATR 构建动态支撑/阻力通道。

    黄金日内交易中广泛使用，能自动切换多空方向并提供动态止损参考。

    输出：
        supertrend  — 当前通道值（上轨或下轨）
        direction   — 1.0 = 多头（bullish），-1.0 = 空头（bearish）
        上穿 direction 由 -1 → 1 时为买入信号，下穿为卖出信号。
    """
    period = get_int(params, "period", default=14, aliases=("atr_period", "window"))
    multiplier = get_float(params, "multiplier", default=3.0, aliases=("mult",))
    # 需要足够历史计算 ATR 并完成 Supertrend 通道收敛
    min_bars_needed = period * 2 + 2
    window = tail_bars(bars, min_bars_needed)
    if len(window) < period + 2:
        return {}

    highs, lows, closes = get_hlc_arrays(window)

    # 向量化计算 True Range（从第 2 根 bar 开始）
    prev_closes = closes[:-1]
    h = highs[1:]
    l = lows[1:]
    trs = np.maximum(h - l, np.maximum(np.abs(h - prev_closes), np.abs(l - prev_closes)))

    # Wilder 平滑 ATR
    n_tr = len(trs)
    if n_tr < period:
        return {}
    atr_val = float(np.mean(trs[:period]))
    atr_series: List[float] = [atr_val]
    for i in range(period, n_tr):
        atr_val = (atr_val * (period - 1) + float(trs[i])) / period
        atr_series.append(atr_val)

    # bars_for_st 与 trs 对齐
    bars_for_st_highs = highs[1:]
    bars_for_st_lows = lows[1:]
    bars_for_st_closes = closes[1:]
    if len(bars_for_st_closes) != n_tr:
        return {}

    aligned_start = period - 1
    if len(bars_for_st_closes) <= aligned_start:
        return {}

    # 迭代计算 Supertrend（方向有序列依赖）
    final_upper = 0.0
    final_lower = 0.0
    direction = 1.0
    supertrend_val = 0.0

    for i in range(len(atr_series)):
        bar_idx = aligned_start + i
        atr_i = atr_series[i]
        bar_h = float(bars_for_st_highs[bar_idx])
        bar_l = float(bars_for_st_lows[bar_idx])
        bar_c = float(bars_for_st_closes[bar_idx])
        hl2 = (bar_h + bar_l) / 2.0
        basic_upper = hl2 + multiplier * atr_i
        basic_lower = hl2 - multiplier * atr_i

        if i == 0:
            final_upper = basic_upper
            final_lower = basic_lower
            direction = -1.0 if bar_c <= final_upper else 1.0
            supertrend_val = final_upper if direction == -1.0 else final_lower
            continue

        prev_close_val = float(bars_for_st_closes[bar_idx - 1])
        prev_final_upper = final_upper
        prev_final_lower = final_lower

        final_upper = (
            min(basic_upper, prev_final_upper)
            if prev_close_val <= prev_final_upper
            else basic_upper
        )
        final_lower = (
            max(basic_lower, prev_final_lower)
            if prev_close_val >= prev_final_lower
            else basic_lower
        )

        prev_direction = direction
        if prev_direction == -1.0:
            direction = 1.0 if bar_c > prev_final_upper else -1.0
        else:
            direction = -1.0 if bar_c < prev_final_lower else 1.0

        supertrend_val = final_lower if direction == 1.0 else final_upper

    return {
        "supertrend": supertrend_val,
        "direction": direction,
        "upper_band": final_upper,
        "lower_band": final_lower,
    }


class SupertrendIncremental(IncrementalIndicator):
    """Incremental Supertrend: ATR (Wilder) + band logic.

    State: atr_val, final_upper, final_lower, direction, prev_close.
    """

    def __init__(self, name: str, params: Dict[str, Any]) -> None:
        super().__init__(name, params)
        period = get_int(params, "period", default=14, aliases=("atr_period", "window"))
        self.min_data_points = period * 2 + 2

    def _compute_full(self, bars: list) -> Dict[str, float]:
        return supertrend(bars, self.params)

    def _can_use_incremental(self, bars: list, state: IndicatorState) -> bool:
        if not super()._can_use_incremental(bars, state):
            return False
        ir = state.intermediate_results
        return (
            ir is not None
            and "atr_val" in ir
            and "final_upper" in ir
            and "final_lower" in ir
            and "direction" in ir
            and "prev_close" in ir
        )

    def _compute_incremental(self, bars: list, state: IndicatorState) -> Dict[str, float]:
        period = get_int(self.params, "period", default=14, aliases=("atr_period", "window"))
        multiplier = get_float(self.params, "multiplier", default=3.0, aliases=("mult",))

        ir = state.intermediate_results
        prev_atr = float(ir["atr_val"])  # type: ignore[index]
        prev_final_upper = float(ir["final_upper"])  # type: ignore[index]
        prev_final_lower = float(ir["final_lower"])  # type: ignore[index]
        prev_direction = float(ir["direction"])  # type: ignore[index]
        prev_close = float(ir["prev_close"])  # type: ignore[index]

        bar = bars[-1]
        tr = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        atr_val = (prev_atr * (period - 1) + tr) / period

        hl2 = (bar.high + bar.low) / 2.0
        basic_upper = hl2 + multiplier * atr_val
        basic_lower = hl2 - multiplier * atr_val

        final_upper = (
            min(basic_upper, prev_final_upper)
            if prev_close <= prev_final_upper
            else basic_upper
        )
        final_lower = (
            max(basic_lower, prev_final_lower)
            if prev_close >= prev_final_lower
            else basic_lower
        )

        if prev_direction == -1.0:
            direction = 1.0 if bar.close > prev_final_upper else -1.0
        else:
            direction = -1.0 if bar.close < prev_final_lower else 1.0

        supertrend_val = final_lower if direction == 1.0 else final_upper

        return {
            "supertrend": supertrend_val,
            "direction": direction,
            "upper_band": final_upper,
            "lower_band": final_lower,
        }

    def _create_new_state(self, bars: list, result: Dict[str, float]) -> IndicatorState:
        state = super()._create_new_state(bars, result)
        full_result = self._compute_full(bars)
        if full_result:
            period = get_int(self.params, "period", default=14, aliases=("atr_period", "window"))
            window = tail_bars(bars, period * 2 + 2)
            highs, lows, closes = get_hlc_arrays(window)
            from .volatility import _compute_tr_array
            trs = _compute_tr_array(highs, lows, closes)
            if len(trs) >= period:
                atr_val = float(np.mean(trs[:period]))
                for i in range(period, len(trs)):
                    atr_val = (atr_val * (period - 1) + float(trs[i])) / period
            else:
                atr_val = 0.0

            state.intermediate_results = {
                "atr_val": atr_val,
                "final_upper": full_result.get("upper_band", 0.0),
                "final_lower": full_result.get("lower_band", 0.0),
                "direction": full_result.get("direction", 1.0),
                "prev_close": bars[-1].close,
            }
        return state


def _rsi_series_wilder(closes: List[float], period: int) -> List[float]:
    """Build a full RSI series using Wilder smoothing (consistent with rsi()).

    Returns one RSI value per close starting from index ``period``.
    """
    if len(closes) <= period:
        return []

    closes_arr = np.array(closes, dtype=np.float64)
    diffs = np.diff(closes_arr)
    gains = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)

    # Seed with SMA of first ``period`` diffs
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))

    series: List[float] = []
    if avg_loss == 0:
        series.append(100.0)
    else:
        rs = avg_gain / avg_loss
        series.append(100.0 - 100.0 / (1.0 + rs))

    # Wilder exponential smoothing for subsequent diffs
    for i in range(period, len(diffs)):
        gain = float(gains[i])
        loss = float(losses[i])
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            series.append(100.0)
        else:
            rs = avg_gain / avg_loss
            series.append(100.0 - 100.0 / (1.0 + rs))

    return series


def stoch_rsi(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    """Stochastic RSI — 对 RSI 序列再做随机化，对超买超卖更敏感。

    黄金日内交易中可以更早发现动量耗尽信号。
    使用 Wilder 平滑计算 RSI 序列（与 rsi() 函数一致）。

    输出：
        stoch_rsi_k  — %K 线（平滑后的随机RSI）
        stoch_rsi_d  — %D 线（K 的 SMA）
    """
    rsi_period = get_int(params, "rsi_period", default=14, aliases=("period",))
    stoch_period = get_int(params, "stoch_period", default=14, aliases=("k_period",))
    smooth_k = get_int(params, "smooth_k", default=3)
    smooth_d = get_int(params, "smooth_d", default=3)

    # Need enough closes for Wilder RSI warmup + stoch window + smoothing
    needed = rsi_period * 3 + stoch_period + smooth_k + smooth_d
    closes = get_closes(bars, needed)
    if len(closes) <= rsi_period + stoch_period:
        return {}

    # Wilder-smoothed RSI series (one value per bar after warmup)
    rsi_series = _rsi_series_wilder(closes, rsi_period)
    if len(rsi_series) < stoch_period:
        return {}

    # 向量化 Stochastic of RSI series
    rsi_arr = np.array(rsi_series, dtype=np.float64)
    raw_k_series: List[float] = []
    for end in range(stoch_period, len(rsi_arr) + 1):
        window_rsi = rsi_arr[end - stoch_period:end]
        lowest = float(np.min(window_rsi))
        highest = float(np.max(window_rsi))
        if highest == lowest:
            raw_k_series.append(50.0)
        else:
            raw_k_series.append((float(rsi_arr[end - 1]) - lowest) / (highest - lowest) * 100.0)

    if not raw_k_series:
        return {}

    # Smooth K (SMA of raw_k)
    k_arr = np.array(raw_k_series, dtype=np.float64)
    k_series: List[float] = []
    for end in range(smooth_k, len(k_arr) + 1):
        k_series.append(float(np.mean(k_arr[end - smooth_k:end])))

    if not k_series:
        return {}

    k_val = k_series[-1]

    # D line (SMA of K)
    d_window = k_series[-smooth_d:] if len(k_series) >= smooth_d else k_series
    d_val = sum(d_window) / len(d_window)

    return {"stoch_rsi_k": k_val, "stoch_rsi_d": d_val}
