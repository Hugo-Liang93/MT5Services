"""价格结构指标 — HH/HL/LH/LL 序列 + Swing Points。

输出字段（均为无量纲或 ATR 归一化）：
  structure_type:          1.0 (HH+HL上升) / -1.0 (LH+LL下降) / 0.0 (混合)
  trend_bars:              连续 higher close 或 lower close 的 bar 数（正=多/负=空）
  dist_to_swing_high_atr:  距最近 swing high 的距离 / ATR
  dist_to_swing_low_atr:   距最近 swing low 的距离 / ATR
  swing_range_atr:         (swing_high - swing_low) / ATR
"""

from typing import Any, Dict, Iterable, List, Optional

from .base import get_int, sanitize_result, tail_bars


def price_structure(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    """识别价格结构模式。"""
    lookback = get_int(params, "lookback", default=20)
    swing_window = get_int(params, "swing_window", default=5)
    atr_period = get_int(params, "atr_period", default=14)
    min_bars = get_int(params, "min_bars", default=25)

    window = tail_bars(bars, lookback + atr_period + 1)
    if len(window) < min_bars:
        return {}

    # ATR 计算
    atr = _compute_atr(window, atr_period)
    if atr < 1e-12:
        return {}

    analysis_bars = window[-lookback:]
    current = analysis_bars[-1]

    # Swing points 检测
    swing_highs: List[float] = []
    swing_lows: List[float] = []
    for i in range(swing_window, len(analysis_bars) - swing_window):
        is_swing_high = all(
            analysis_bars[i].high >= analysis_bars[j].high
            for j in range(i - swing_window, i + swing_window + 1)
            if j != i
        )
        if is_swing_high:
            swing_highs.append(analysis_bars[i].high)

        is_swing_low = all(
            analysis_bars[i].low <= analysis_bars[j].low
            for j in range(i - swing_window, i + swing_window + 1)
            if j != i
        )
        if is_swing_low:
            swing_lows.append(analysis_bars[i].low)

    # Structure type: HH+HL vs LH+LL
    structure_type = 0.0
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        hh = swing_highs[-1] > swing_highs[-2]
        hl = swing_lows[-1] > swing_lows[-2]
        lh = swing_highs[-1] < swing_highs[-2]
        ll = swing_lows[-1] < swing_lows[-2]
        if hh and hl:
            structure_type = 1.0  # 上升结构
        elif lh and ll:
            structure_type = -1.0  # 下降结构

    # Trend bars: 连续 higher/lower close
    trend_bars = 0
    if len(analysis_bars) >= 2:
        direction = 1 if current.close > analysis_bars[-2].close else -1
        trend_bars = direction
        for i in range(len(analysis_bars) - 2, 0, -1):
            if direction > 0 and analysis_bars[i].close > analysis_bars[i - 1].close:
                trend_bars += 1
            elif direction < 0 and analysis_bars[i].close < analysis_bars[i - 1].close:
                trend_bars -= 1
            else:
                break

    # 距 swing points 的距离
    dist_high = 0.0
    dist_low = 0.0
    swing_range = 0.0

    if swing_highs:
        dist_high = (swing_highs[-1] - current.close) / atr
    if swing_lows:
        dist_low = (current.close - swing_lows[-1]) / atr
    if swing_highs and swing_lows:
        swing_range = (swing_highs[-1] - swing_lows[-1]) / atr

    return sanitize_result(
        {
            "structure_type": structure_type,
            "trend_bars": float(trend_bars),
            "dist_to_swing_high_atr": round(dist_high, 4),
            "dist_to_swing_low_atr": round(dist_low, 4),
            "swing_range_atr": round(swing_range, 4),
        }
    )


def _compute_atr(bars: list, period: int) -> float:
    """简单 ATR 计算。"""
    if len(bars) < period + 1:
        return 0.0
    tr_sum = 0.0
    for i in range(len(bars) - period, len(bars)):
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close),
        )
        tr_sum += tr
    return tr_sum / period
