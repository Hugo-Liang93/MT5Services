"""Daily Pivots — 经典浮动 pivot points (P/R1/S1/R2/S2)。

Day-trading 经典 SR：retail 普遍参考 pivot levels 决策入场/出场。
基于前一交易日 OHLC 的标准 floor pivot 公式：

    P  = (prev_H + prev_L + prev_C) / 3
    R1 = 2P - prev_L           S1 = 2P - prev_H
    R2 = P + (prev_H - prev_L) S2 = P - (prev_H - prev_L)

输出字段：
    pivot, r1, s1, r2, s2          5 个 pivot levels
    distance_to_pivot              close - P（>0 在 P 之上）
    distance_to_r1, distance_to_s1 close - R1, close - S1
    nearest_level_name             {"pivot", "r1", "s1", "r2", "s2"}
    nearest_level_distance         |close - nearest_price|（绝对距离）

边界处理：
- bars 为空 / 无前一日 → {}
- 周末跳过：自动取最近可用过去日（按 UTC 日期）
- 实现复用 prior_day_levels 同样的"倒序找前一日 OHLC"逻辑
"""

from typing import Any, Dict, Iterable

from src.clients.mt5_market import OHLC


def daily_pivots(bars: Iterable[OHLC], params: Dict[str, Any]) -> Dict[str, float]:
    """计算前一交易日 floor pivots + 当前 close 与各 level 的距离。"""
    bars_list = list(bars) if not isinstance(bars, list) else bars
    if not bars_list:
        return {}

    current = bars_list[-1]
    current_date = current.time.date()

    # 倒序扫描找前一日 OHLC（与 prior_day_levels 相同逻辑——保持一致防漂移）
    prev_day_date = None
    prev_high: float | None = None
    prev_low: float | None = None
    prev_close: float | None = None
    for bar in reversed(bars_list[:-1]):
        bar_date = bar.time.date()
        if bar_date == current_date:
            continue
        if prev_day_date is None:
            prev_day_date = bar_date
            prev_close = float(bar.close)
            prev_high = float(bar.high)
            prev_low = float(bar.low)
            continue
        if bar_date != prev_day_date:
            break
        if bar.high > (prev_high or float("-inf")):
            prev_high = float(bar.high)
        if bar.low < (prev_low or float("inf")):
            prev_low = float(bar.low)

    if prev_high is None or prev_low is None or prev_close is None:
        return {}

    pivot = (prev_high + prev_low + prev_close) / 3.0
    r1 = 2.0 * pivot - prev_low
    s1 = 2.0 * pivot - prev_high
    range_ = prev_high - prev_low
    r2 = pivot + range_
    s2 = pivot - range_

    close_now = float(current.close)
    levels = {"pivot": pivot, "r1": r1, "s1": s1, "r2": r2, "s2": s2}
    distances = {name: close_now - price for name, price in levels.items()}
    nearest_name = min(distances, key=lambda k: abs(distances[k]))
    nearest_distance = abs(distances[nearest_name])

    return {
        "pivot": round(pivot, 4),
        "r1": round(r1, 4),
        "s1": round(s1, 4),
        "r2": round(r2, 4),
        "s2": round(s2, 4),
        "distance_to_pivot": round(distances["pivot"], 4),
        "distance_to_r1": round(distances["r1"], 4),
        "distance_to_s1": round(distances["s1"], 4),
        "nearest_level_name": nearest_name,
        "nearest_level_distance": round(nearest_distance, 4),
    }
