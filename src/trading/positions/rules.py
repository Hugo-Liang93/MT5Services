"""持仓管理纯逻辑：breakeven / trailing stop / trailing TP / 日终平仓判定。

这些函数不依赖 MT5 API，可被实盘 PositionManager 和回测 PortfolioTracker 共用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class BreakevenResult:
    should_apply: bool
    new_stop_loss: Optional[float] = None


@dataclass
class TrailingStopResult:
    should_update: bool
    new_stop_loss: Optional[float] = None


@dataclass
class TrailingTakeProfitResult:
    should_update: bool
    new_take_profit: Optional[float] = None


@dataclass
class EndOfDayResult:
    should_close: bool


def check_breakeven(
    action: str,
    entry_price: float,
    current_price: float,
    atr_at_entry: float,
    breakeven_atr_threshold: float,
    already_applied: bool,
) -> BreakevenResult:
    if already_applied:
        return BreakevenResult(should_apply=False)

    threshold = atr_at_entry * breakeven_atr_threshold
    if action == "buy" and current_price >= entry_price + threshold:
        return BreakevenResult(should_apply=True, new_stop_loss=entry_price + 0.01)
    if action == "sell" and current_price <= entry_price - threshold:
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


def check_trailing_take_profit(
    action: str,
    entry_price: float,
    current_take_profit: float,
    atr_at_entry: float,
    activation_atr: float,
    trail_atr: float,
    highest_price: Optional[float],
    lowest_price: Optional[float],
) -> TrailingTakeProfitResult:
    activation_distance = atr_at_entry * activation_atr
    trail_distance = atr_at_entry * trail_atr

    if action == "buy" and highest_price is not None:
        profit = highest_price - entry_price
        if profit >= activation_distance:
            new_tp = highest_price - trail_distance
            if new_tp <= entry_price:
                return TrailingTakeProfitResult(should_update=False)
            if new_tp < current_take_profit:
                return TrailingTakeProfitResult(should_update=True, new_take_profit=new_tp)

    elif action == "sell" and lowest_price is not None:
        profit = entry_price - lowest_price
        if profit >= activation_distance:
            new_tp = lowest_price + trail_distance
            if new_tp >= entry_price:
                return TrailingTakeProfitResult(should_update=False)
            if new_tp > current_take_profit:
                return TrailingTakeProfitResult(should_update=True, new_take_profit=new_tp)

    return TrailingTakeProfitResult(should_update=False)


def should_close_end_of_day(
    current_time: datetime,
    close_hour_utc: int,
    close_minute_utc: int,
    last_close_date: Optional[str],
) -> EndOfDayResult:
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
