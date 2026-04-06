"""Bar 统计指标 — 当前 bar 的 body/range 相对于历史均值的统计量。

输出字段：
  body_ratio:      当前 bar body / 过去 N bar 平均 body（>1 = 大 bar）
  range_ratio:     当前 bar range / 过去 N bar 平均 range
  close_position:  收盘在 bar range 中的位置（0=最低, 1=最高）
  is_bullish:      1.0 if close > open else -1.0（方向标记）

用途：BarMomentumSurge 策略消费此指标，检测"大 bar 动量爆发"。
"""

from typing import Any, Dict, Iterable

from .base import get_int, tail_bars


def bar_stats(bars: Iterable, params: Dict[str, Any]) -> Dict[str, float]:
    """计算当前 bar 的 body/range 统计量。"""
    lookback = get_int(params, "period", default=20)
    window = tail_bars(bars, lookback + 1)

    if len(window) < lookback + 1:
        return {}

    current = window[-1]
    history = window[:-1]

    body = abs(current.close - current.open)
    bar_range = current.high - current.low

    if bar_range <= 0:
        return {}

    # 历史平均 body 和 range
    avg_body = sum(abs(b.close - b.open) for b in history) / len(history)
    avg_range = sum(b.high - b.low for b in history) / len(history)

    body_ratio = body / avg_body if avg_body > 0 else 0.0
    range_ratio = bar_range / avg_range if avg_range > 0 else 0.0
    close_position = (current.close - current.low) / bar_range
    is_bullish = 1.0 if current.close > current.open else -1.0

    return {
        "body_ratio": round(body_ratio, 4),
        "range_ratio": round(range_ratio, 4),
        "close_position": round(close_position, 4),
        "is_bullish": is_bullish,
    }
