"""复合指标 — 从已有指标逻辑派生的二级指标。

从 feature_engineer 提升为正式指标，实盘/回测/研究三路可用。
所有输出均为无量纲，兼容 rule_mining。
"""

from typing import Any, Dict, Iterable

import numpy as np

from .base import get_float, get_int, sanitize_result, tail_bars


def di_spread(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    """方向指数差异：(+DI - -DI) / (+DI + -DI + ε)。

    输出: di_spread ∈ [-1, 1]
      > 0 多头占优，< 0 空头占优，绝对值越大方向越明确。
    """
    period = get_int(params, "period", default=14)
    min_bars = get_int(params, "min_bars", default=20)
    window = tail_bars(bars, min_bars)
    if len(window) < min_bars:
        return {}

    # 计算 True Range 和 DM
    plus_dm_sum = 0.0
    minus_dm_sum = 0.0
    tr_sum = 0.0

    for i in range(1, len(window)):
        high = window[i].high
        low = window[i].low
        prev_high = window[i - 1].high
        prev_low = window[i - 1].low
        prev_close = window[i - 1].close

        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_sum += tr

        up_move = high - prev_high
        down_move = prev_low - low

        if up_move > down_move and up_move > 0:
            plus_dm_sum += up_move
        if down_move > up_move and down_move > 0:
            minus_dm_sum += down_move

    if tr_sum < 1e-12:
        return {}

    plus_di = 100.0 * plus_dm_sum / tr_sum
    minus_di = 100.0 * minus_dm_sum / tr_sum
    spread = (plus_di - minus_di) / (plus_di + minus_di + 1e-6)

    return sanitize_result({"di_spread": round(spread, 4)})


def squeeze_detector(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    """波动率挤压检测：BB width < KC width → squeeze。

    输出:
      squeeze: 1.0 (挤压中) / 0.0 (非挤压)
      squeeze_intensity: KC_width / BB_width - 1（挤压强度，越大越紧）
    """
    bb_period = get_int(params, "bb_period", default=20)
    kc_period = get_int(params, "kc_period", default=20)
    bb_std = get_float(params, "bb_std", default=2.0)
    kc_atr_mult = get_float(params, "kc_atr_mult", default=1.5)
    min_bars = get_int(params, "min_bars", default=21)

    window = tail_bars(bars, max(bb_period, kc_period) + 1)
    if len(window) < min_bars:
        return {}

    closes = np.array([b.close for b in window[-bb_period:]], dtype=np.float64)
    sma = float(np.mean(closes))
    std = float(np.std(closes, ddof=1)) if len(closes) > 1 else 0.0

    bb_upper = sma + bb_std * std
    bb_lower = sma - bb_std * std
    bb_width = bb_upper - bb_lower

    # Keltner Channel: EMA + ATR * mult
    atr_sum = 0.0
    for i in range(1, len(window)):
        tr = max(
            window[i].high - window[i].low,
            abs(window[i].high - window[i - 1].close),
            abs(window[i].low - window[i - 1].close),
        )
        atr_sum += tr
    atr = atr_sum / (len(window) - 1) if len(window) > 1 else 0.0
    kc_width = 2.0 * kc_atr_mult * atr

    if bb_width < 1e-12:
        return {}

    is_squeeze = 1.0 if bb_width < kc_width else 0.0
    intensity = (kc_width / bb_width - 1.0) if is_squeeze else 0.0

    return sanitize_result(
        {
            "squeeze": is_squeeze,
            "squeeze_intensity": round(intensity, 4),
        }
    )


def vwap_deviation(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    """VWAP 偏离度：(close - VWAP) / ATR。

    输出: vwap_gap_atr — 无量纲，正=高于公允价值，负=低于。
    """
    vwap_period = get_int(params, "vwap_period", default=30)
    atr_period = get_int(params, "atr_period", default=14)
    min_bars = get_int(params, "min_bars", default=30)

    window = tail_bars(bars, max(vwap_period, atr_period) + 1)
    if len(window) < min_bars:
        return {}

    # VWAP
    vwap_bars = window[-vwap_period:]
    total_pv = sum((b.high + b.low + b.close) / 3.0 * b.volume for b in vwap_bars)
    total_vol = sum(b.volume for b in vwap_bars)
    vwap = total_pv / total_vol if total_vol > 0 else 0.0

    # ATR
    atr_sum = 0.0
    atr_bars = window[-(atr_period + 1) :]
    for i in range(1, len(atr_bars)):
        tr = max(
            atr_bars[i].high - atr_bars[i].low,
            abs(atr_bars[i].high - atr_bars[i - 1].close),
            abs(atr_bars[i].low - atr_bars[i - 1].close),
        )
        atr_sum += tr
    atr = atr_sum / atr_period if atr_period > 0 else 0.0

    if atr < 1e-12 or vwap < 1e-12:
        return {}

    current_close = window[-1].close
    gap = (current_close - vwap) / atr

    return sanitize_result({"vwap_gap_atr": round(gap, 4)})


def momentum_accel(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    """动量加速度：RSI 和 ROC 的二阶导。

    输出:
      rsi_accel: RSI 的变化速度的变化（正=加速上行，负=加速下行）
      roc_accel: ROC 的变化速度的变化
    """
    rsi_period = get_int(params, "rsi_period", default=14)
    roc_period = get_int(params, "roc_period", default=12)
    delta = get_int(params, "delta_bars", default=3)
    min_bars = get_int(params, "min_bars", default=20)

    # 需要足够多的 bars 来计算两次 delta
    total_needed = max(rsi_period, roc_period) + delta * 2 + 2
    window = tail_bars(bars, total_needed)
    if len(window) < min_bars:
        return {}

    closes = [b.close for b in window]
    result: Dict[str, float] = {}

    # RSI 加速度
    rsi_values = _compute_rsi_series(closes, rsi_period)
    if len(rsi_values) >= delta * 2 + 1:
        rsi_d1_cur = rsi_values[-1] - rsi_values[-1 - delta]
        rsi_d1_prev = rsi_values[-1 - delta] - rsi_values[-1 - delta * 2]
        result["rsi_accel"] = round(rsi_d1_cur - rsi_d1_prev, 4)

    # ROC 加速度
    if len(closes) >= roc_period + delta * 2 + 1:
        roc_values = []
        for i in range(roc_period, len(closes)):
            prev = closes[i - roc_period]
            roc_values.append((closes[i] - prev) / prev * 100.0 if prev > 0 else 0.0)
        if len(roc_values) >= delta * 2 + 1:
            roc_d1_cur = roc_values[-1] - roc_values[-1 - delta]
            roc_d1_prev = roc_values[-1 - delta] - roc_values[-1 - delta * 2]
            result["roc_accel"] = round(roc_d1_cur - roc_d1_prev, 4)

    return sanitize_result(result) if result else {}


def _compute_rsi_series(closes: list, period: int) -> list:
    """计算 RSI 序列（用于加速度计算）。"""
    if len(closes) < period + 1:
        return []
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    rsi_values = []
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        if i > period:
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss < 1e-12:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(100.0 - 100.0 / (1.0 + rs))

    return rsi_values
