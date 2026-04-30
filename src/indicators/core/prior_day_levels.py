"""Prior Day Levels — 前一交易日 OHLC 水平。

Day-trading edge：retail trader 普遍参考"昨日高/低/收盘"作为 SR levels，
价格触及 + 反转 K 线 = 经典反转入场（参 #4 Prior Day H/L Retest 策略）。

输出字段：
    prev_day_high            上一交易日最高价
    prev_day_low             上一交易日最低价
    prev_day_close           上一交易日最后一根 bar 的 close（≠ max/min）
    prev_day_range           prev_day_high - prev_day_low
    position_in_prev_range   当前 close 在昨日区间的归一化位置 [0, 1]
                              （0 = 等于昨日 low，1 = 等于昨日 high）
    distance_to_prev_high    prev_day_high - current_close（>0 表示在昨高之下）
    distance_to_prev_low     current_close - prev_day_low（>0 表示在昨低之上）

边界处理：
- bars 为空 → {}
- 仅有当日 bars（无前一日）→ {}
- 跳过周末 / 节假日：自动取最近一个有数据的过去日（按 UTC 日期）
- 昨日 high == low（极端窄幅）→ position_in_prev_range 安全退化为 0.5
"""

from typing import Any, Dict, Iterable

from src.clients.mt5_market import OHLC


def prior_day_levels(bars: Iterable[OHLC], params: Dict[str, Any]) -> Dict[str, float]:
    """计算前一交易日 OHLC 水平 + 当前 close 与之的相对位置。"""
    bars_list = list(bars) if not isinstance(bars, list) else bars
    if not bars_list:
        return {}

    current = bars_list[-1]
    current_date = current.time.date()

    # 倒序扫描找前一日 bars。第一根遇到 date != current_date 的 bar 即为
    # 昨日最后一根（reversed walk 中最先出现的非当日 bar）。继续往前直到
    # date 再变化（前前日开始）即停。
    prev_day_date = None
    prev_day_high: float | None = None
    prev_day_low: float | None = None
    prev_day_close: float | None = None
    for bar in reversed(bars_list[:-1]):
        bar_date = bar.time.date()
        if bar_date == current_date:
            continue
        if prev_day_date is None:
            prev_day_date = bar_date
            # 此根在 reversed 顺序下是当日最后一根 → close 是收盘
            prev_day_close = float(bar.close)
            prev_day_high = float(bar.high)
            prev_day_low = float(bar.low)
            continue
        if bar_date != prev_day_date:
            break
        # 同一前日的更早 bar：累积 H/L
        if bar.high > (prev_day_high or float("-inf")):
            prev_day_high = float(bar.high)
        if bar.low < (prev_day_low or float("inf")):
            prev_day_low = float(bar.low)

    if prev_day_high is None or prev_day_low is None or prev_day_close is None:
        return {}

    prev_range = prev_day_high - prev_day_low
    close_now = float(current.close)

    if prev_range > 0:
        position = (close_now - prev_day_low) / prev_range
        # 严格 clip 到 [0, 1] 避免轻微数值误差或当前 bar 突破昨日区间时溢出
        position = max(0.0, min(1.0, position))
    else:
        position = 0.5  # 昨日全平 bar，无意义区间

    return {
        "prev_day_high": prev_day_high,
        "prev_day_low": prev_day_low,
        "prev_day_close": prev_day_close,
        "prev_day_range": prev_range,
        "position_in_prev_range": round(position, 4),
        "distance_to_prev_high": round(prev_day_high - close_now, 4),
        "distance_to_prev_low": round(close_now - prev_day_low, 4),
    }
