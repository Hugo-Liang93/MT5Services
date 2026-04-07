"""K 线形态指标 — 识别经典蜡烛图形态。

输出字段（均为无量纲）：
  hammer:         1.0 (看多锤子) / -1.0 (倒锤子/上吊) / 0.0
  engulfing:      1.0 (看涨吞没) / -1.0 (看跌吞没) / 0.0
  doji:           1.0 (十字星) / 0.0
  pin_bar:        1.0 (多头 pin bar) / -1.0 (空头 pin bar) / 0.0
  inside_bar:     1.0 (内包线) / 0.0
  consecutive_dir: 正=连续阳线数 / 负=连续阴线数
"""

from typing import Any, Dict, Iterable

from .base import get_int, sanitize_result, tail_bars


def candlestick_patterns(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    """识别经典 K 线形态。"""
    min_bars = get_int(params, "min_bars", default=3)
    window = tail_bars(bars, max(min_bars, 10))  # 需要回看连续方向

    if len(window) < 2:
        return {}

    cur = window[-1]
    prev = window[-2]

    body = abs(cur.close - cur.open)
    bar_range = cur.high - cur.low
    upper_shadow = cur.high - max(cur.close, cur.open)
    lower_shadow = min(cur.close, cur.open) - cur.low
    is_bull = cur.close > cur.open

    if bar_range < 1e-12:
        return {}

    # ── Hammer / Inverted Hammer ───────────────────────────────
    hammer = 0.0
    if body > 0:
        if lower_shadow > 2.0 * body and upper_shadow < body * 0.3:
            hammer = 1.0  # 多头锤子（下影长、上影短）
        elif upper_shadow > 2.0 * body and lower_shadow < body * 0.3:
            hammer = -1.0  # 倒锤子 / 上吊线

    # ── Engulfing ──────────────────────────────────────────────
    engulfing = 0.0
    prev_body_top = max(prev.close, prev.open)
    prev_body_bot = min(prev.close, prev.open)
    cur_body_top = max(cur.close, cur.open)
    cur_body_bot = min(cur.close, cur.open)

    if cur_body_top > prev_body_top and cur_body_bot < prev_body_bot:
        prev_bull = prev.close > prev.open
        if is_bull and not prev_bull:
            engulfing = 1.0  # 看涨吞没
        elif not is_bull and prev_bull:
            engulfing = -1.0  # 看跌吞没

    # ── Doji ───────────────────────────────────────────────────
    doji = 1.0 if body < bar_range * 0.1 else 0.0

    # ── Pin Bar ────────────────────────────────────────────────
    pin_bar = 0.0
    if body > 0:
        if lower_shadow > 2.5 * body and upper_shadow < body * 0.2:
            pin_bar = 1.0  # 多头 pin bar
        elif upper_shadow > 2.5 * body and lower_shadow < body * 0.2:
            pin_bar = -1.0  # 空头 pin bar

    # ── Inside Bar ─────────────────────────────────────────────
    inside_bar = 0.0
    if cur.high < prev.high and cur.low > prev.low:
        inside_bar = 1.0

    # ── Consecutive Direction ──────────────────────────────────
    consecutive = 0
    if len(window) >= 2:
        cur_dir = 1 if cur.close > cur.open else -1
        consecutive = cur_dir
        for i in range(len(window) - 2, -1, -1):
            b = window[i]
            b_dir = 1 if b.close > b.open else -1
            if b_dir == cur_dir:
                consecutive += cur_dir
            else:
                break

    return sanitize_result(
        {
            "hammer": hammer,
            "engulfing": engulfing,
            "doji": doji,
            "pin_bar": pin_bar,
            "inside_bar": inside_bar,
            "consecutive_dir": float(consecutive),
        }
    )
