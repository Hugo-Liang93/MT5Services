from typing import Any, Dict, Iterable, List

from .base import get_closes, get_int, tail_bars
from .mean import _ema_sequence


def rsi(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    """RSI — 使用 Wilder 平滑（标准实现）。

    Bug修复：原实现仅用最后 period+1 根 bar 做简单平均初始化，
    导致初始阶段 RSI 偏差较大。改为用更多历史数据（period*3）做
    Wilder 指数平滑，与主流交易软件（MT5、TradingView）结果一致。
    """
    period = get_int(params, "period", default=14, aliases=("window",))
    # 使用更多历史数据让 Wilder 平滑收敛
    closes = get_closes(bars, period * 3 + 1)
    if len(closes) <= period:
        return {}

    # 首 period 根 bar 用简单平均初始化 avg_gain / avg_loss（Wilder 标准做法）
    diffs = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
    avg_gain = sum(d for d in diffs[:period] if d > 0) / period
    avg_loss = sum(-d for d in diffs[:period] if d < 0) / period

    # Wilder 平滑余下的数据点
    for diff in diffs[period:]:
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return {"rsi": 100.0}
    rs = avg_gain / avg_loss
    rsi_val = 100.0 - (100.0 / (1.0 + rs))
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


def stochastic(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    k_period = get_int(params, "k_period", default=14, aliases=("period",))
    d_period = get_int(params, "d_period", default=3)
    window = tail_bars(bars, k_period + d_period - 1)
    if len(window) < k_period:
        return {}

    k_values: List[float] = []
    for end_idx in range(k_period, len(window) + 1):
        sample = window[:end_idx][-k_period:]
        highest_high = max(bar.high for bar in sample)
        lowest_low = min(bar.low for bar in sample)
        if highest_high == lowest_low:
            k_values.append(50.0)
            continue
        k_values.append((sample[-1].close - lowest_low) / (highest_high - lowest_low) * 100.0)

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
    highest_high = max(bar.high for bar in window)
    lowest_low = min(bar.low for bar in window)
    if highest_high == lowest_low:
        return {"williams_r": 0.0}
    value = (highest_high - window[-1].close) / (highest_high - lowest_low) * -100.0
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

    # 计算每根 K 线的 True Range
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

    # Wilder 平滑 ATR
    if len(trs) < period:
        return {}
    atr_val = sum(trs[:period]) / period
    atr_series: List[float] = [atr_val]
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
        atr_series.append(atr_val)

    bars_for_st = window[1:]  # 与 trs 对齐，去掉第一根（用于计算首个 TR）
    if len(bars_for_st) != len(trs):
        return {}

    # 把 ATR 序列对齐到 bars（atr_series[i] 对应 bars_for_st[period + i - 1]）
    # 只需要 atr_series 中有值的区间
    aligned_start = period - 1  # bars_for_st 中第一根有 ATR 的索引
    if len(bars_for_st) <= aligned_start:
        return {}

    # 迭代计算 Supertrend
    final_upper = 0.0
    final_lower = 0.0
    direction = 1.0
    supertrend_val = 0.0

    for i, bar in enumerate(bars_for_st[aligned_start:]):
        atr_i = atr_series[i]
        hl2 = (bar.high + bar.low) / 2.0
        basic_upper = hl2 + multiplier * atr_i
        basic_lower = hl2 - multiplier * atr_i

        if i == 0:
            final_upper = basic_upper
            final_lower = basic_lower
            # 首轮方向由收盘价与基础上轨比较决定
            direction = -1.0 if bar.close <= final_upper else 1.0
            supertrend_val = final_upper if direction == -1.0 else final_lower
            continue

        # 上轨：仅在前收盘价高于前最终上轨时允许上轨上移（限制下行更新）
        prev_close_val = bars_for_st[aligned_start + i - 1].close
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


def stoch_rsi(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    """Stochastic RSI — 对 RSI 序列再做随机化，对超买超卖更敏感。

    黄金日内交易中可以更早发现动量耗尽信号。

    输出：
        stoch_rsi_k  — %K 线（平滑后的随机RSI）
        stoch_rsi_d  — %D 线（K 的 SMA）
    """
    rsi_period = get_int(params, "rsi_period", default=14, aliases=("period",))
    stoch_period = get_int(params, "stoch_period", default=14, aliases=("k_period",))
    smooth_k = get_int(params, "smooth_k", default=3)
    smooth_d = get_int(params, "smooth_d", default=3)

    # 需要足够 bar 数构建 RSI 序列
    needed = rsi_period + stoch_period + smooth_k + smooth_d + 5
    closes = get_closes(bars, needed)
    if len(closes) < rsi_period + stoch_period:
        return {}

    # 计算 RSI 序列
    rsi_series: List[float] = []
    for start in range(len(closes) - rsi_period):
        segment = closes[start: start + rsi_period + 1]
        gains: List[float] = []
        losses: List[float] = []
        for prev_p, curr_p in zip(segment[:-1], segment[1:]):
            diff = curr_p - prev_p
            if diff >= 0:
                gains.append(diff)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(-diff)
        avg_gain = sum(gains) / rsi_period
        avg_loss = sum(losses) / rsi_period
        if avg_loss == 0:
            rsi_series.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_series.append(100.0 - 100.0 / (1.0 + rs))

    if len(rsi_series) < stoch_period:
        return {}

    # 对 RSI 序列做随机化
    raw_k_series: List[float] = []
    for end in range(stoch_period, len(rsi_series) + 1):
        window_rsi = rsi_series[end - stoch_period: end]
        lowest = min(window_rsi)
        highest = max(window_rsi)
        if highest == lowest:
            raw_k_series.append(50.0)
        else:
            raw_k_series.append((rsi_series[end - 1] - lowest) / (highest - lowest) * 100.0)

    if not raw_k_series:
        return {}

    # 平滑 K 线（SMA smooth_k）
    k_series: List[float] = []
    for end in range(smooth_k, len(raw_k_series) + 1):
        k_series.append(sum(raw_k_series[end - smooth_k: end]) / smooth_k)

    if not k_series:
        return {}

    k_val = k_series[-1]

    # D 线（K 的 SMA smooth_d）
    d_window = k_series[-smooth_d:] if len(k_series) >= smooth_d else k_series
    d_val = sum(d_window) / len(d_window)

    return {"stoch_rsi_k": k_val, "stoch_rsi_d": d_val}
