"""跳空分析指标 — 当前 open 与前一 bar close 的差异。

输出字段（均为无量纲）：
  gap_atr:      (open - prev_close) / ATR，正=跳高，负=跳低
  gap_fill_pct: 跳空回补百分比 [0, 1]（当前 bar 内回补了多少跳空）
  has_gap:      1.0 if |gap_atr| > threshold else 0.0
"""

from typing import Any, Dict, Iterable

from .base import get_float, get_int, sanitize_result, tail_bars


def gap_analysis(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    """跳空分析。"""
    atr_period = get_int(params, "atr_period", default=14)
    gap_threshold = get_float(params, "gap_threshold_atr", default=0.3)
    min_bars = get_int(params, "min_bars", default=15)

    window = tail_bars(bars, atr_period + 2)
    if len(window) < min_bars:
        return {}

    cur = window[-1]
    prev = window[-2]

    # ATR
    atr_sum = 0.0
    for i in range(1, len(window) - 1):
        tr = max(
            window[i].high - window[i].low,
            abs(window[i].high - window[i - 1].close),
            abs(window[i].low - window[i - 1].close),
        )
        atr_sum += tr
    atr = atr_sum / (len(window) - 2) if len(window) > 2 else 0.0

    if atr < 1e-12:
        return {}

    gap = cur.open - prev.close
    gap_atr = gap / atr

    # Gap fill: 当前 bar 内回补了多少跳空
    gap_fill_pct = 0.0
    if abs(gap) > 1e-12:
        if gap > 0:
            # 跳高：回补 = bar 内跌回跳空区域的比例
            fill = max(0.0, cur.open - cur.low)
            gap_fill_pct = min(1.0, fill / gap)
        else:
            # 跳低：回补 = bar 内涨回跳空区域的比例
            fill = max(0.0, cur.high - cur.open)
            gap_fill_pct = min(1.0, fill / abs(gap))

    has_gap = 1.0 if abs(gap_atr) > gap_threshold else 0.0

    return sanitize_result(
        {
            "gap_atr": round(gap_atr, 4),
            "gap_fill_pct": round(gap_fill_pct, 4),
            "has_gap": has_gap,
        }
    )
