"""持仓管理纯逻辑：breakeven / trailing stop / 日终平仓判定。

这些函数不依赖 MT5 API，可被实盘 PositionManager 和回测 PortfolioTracker 共用。
实盘调用后执行 _modify_sl()（MT5 API），回测调用后直接修改内存中的 SL。

设计原则：回测使用实盘方法，不重新实现，避免模拟失真。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class BreakevenResult:
    """Breakeven 判定结果。"""

    should_apply: bool
    new_stop_loss: Optional[float] = None


@dataclass
class TrailingStopResult:
    """Trailing stop 判定结果。"""

    should_update: bool
    new_stop_loss: Optional[float] = None


@dataclass
class EndOfDayResult:
    """日终平仓判定结果。"""

    should_close: bool


def check_breakeven(
    action: str,
    entry_price: float,
    current_price: float,
    atr_at_entry: float,
    breakeven_atr_threshold: float,
    already_applied: bool,
) -> BreakevenResult:
    """判断是否应将止损移至保本价。

    当价格朝持仓方向移动 >= atr_at_entry * breakeven_atr_threshold 时触发。

    Args:
        action: "buy" 或 "sell"
        entry_price: 入场价
        current_price: 当前价格
        atr_at_entry: 入场时的 ATR 值
        breakeven_atr_threshold: ATR 倍数阈值
        already_applied: 是否已经应用过 breakeven

    Returns:
        BreakevenResult，should_apply=True 时 new_stop_loss 为新的止损价
    """
    if already_applied:
        return BreakevenResult(should_apply=False)

    threshold = atr_at_entry * breakeven_atr_threshold

    if action == "buy" and current_price >= entry_price + threshold:
        return BreakevenResult(should_apply=True, new_stop_loss=entry_price + 0.01)
    elif action == "sell" and current_price <= entry_price - threshold:
        return BreakevenResult(should_apply=True, new_stop_loss=entry_price - 0.01)

    return BreakevenResult(should_apply=False)


def check_trailing_stop(
    action: str,
    current_stop_loss: float,
    atr_at_entry: float,
    trailing_atr_multiplier: float,
    breakeven_applied: bool,
    highest_price: Optional[float],
    lowest_price: Optional[float],
) -> TrailingStopResult:
    """判断是否应移动跟踪止损。

    前提：breakeven 已应用。跟踪距离 = atr_at_entry * trailing_atr_multiplier。

    Args:
        action: "buy" 或 "sell"
        current_stop_loss: 当前止损价
        atr_at_entry: 入场时的 ATR 值
        trailing_atr_multiplier: ATR 倍数
        breakeven_applied: 是否已应用 breakeven（未应用则不触发 trailing）
        highest_price: 持仓期间最高价（多头用）
        lowest_price: 持仓期间最低价（空头用）

    Returns:
        TrailingStopResult，should_update=True 时 new_stop_loss 为新的止损价
    """
    if not breakeven_applied:
        return TrailingStopResult(should_update=False)

    trail_distance = atr_at_entry * trailing_atr_multiplier

    if action == "buy" and highest_price is not None:
        new_sl = highest_price - trail_distance
        if new_sl > current_stop_loss:
            return TrailingStopResult(should_update=True, new_stop_loss=new_sl)

    elif action == "sell" and lowest_price is not None:
        new_sl = lowest_price + trail_distance
        if new_sl < current_stop_loss:
            return TrailingStopResult(should_update=True, new_stop_loss=new_sl)

    return TrailingStopResult(should_update=False)


def should_close_end_of_day(
    current_time: datetime,
    close_hour_utc: int,
    close_minute_utc: int,
    last_close_date: Optional[str],
) -> EndOfDayResult:
    """判断是否到达日终平仓时间。

    Args:
        current_time: 当前时间（必须有时区信息，或视为 UTC）
        close_hour_utc: 平仓时间（UTC 小时）
        close_minute_utc: 平仓时间（UTC 分钟）
        last_close_date: 上次执行平仓的日期字符串（ISO 格式），防止同日重复

    Returns:
        EndOfDayResult，should_close=True 时应执行全部平仓
    """
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    else:
        current_time = current_time.astimezone(timezone.utc)

    closeout_time = current_time.replace(
        hour=close_hour_utc,
        minute=close_minute_utc,
        second=0,
        microsecond=0,
    )
    if current_time < closeout_time:
        return EndOfDayResult(should_close=False)

    day_key = current_time.date().isoformat()
    if last_close_date == day_key:
        return EndOfDayResult(should_close=False)

    return EndOfDayResult(should_close=True)
