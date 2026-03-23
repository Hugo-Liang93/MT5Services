"""组合跟踪器：模拟持仓管理，复用实盘 position_rules 纯逻辑。

设计原则：回测使用实盘方法，不重新实现。
- SL/TP 触发检测：回测独有（实盘由 MT5 服务器执行）
- breakeven / trailing stop 判定：复用 src/trading/position_rules
- 日终平仓判定：复用 src/trading/position_rules
- PnL 计算：与实盘 _close_position 逻辑一致
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

from src.clients.mt5_market import OHLC
from src.trading.position_rules import (
    check_breakeven,
    check_trailing_stop,
    should_close_end_of_day,
)
from src.trading.sizing import TradeParameters

from .models import TradeRecord

logger = logging.getLogger(__name__)


@dataclass
class _Position:
    """内部持仓表示。"""

    signal_id: str
    strategy: str
    action: str  # "buy" | "sell"
    entry_time: datetime
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    regime: str
    confidence: float
    entry_bar_index: int  # 开仓时的 bar 索引（用于计算 bars_held）
    atr_at_entry: float = 0.0
    # breakeven / trailing 状态（与实盘 TrackedPosition 一致）
    breakeven_applied: bool = False
    trailing_active: bool = False
    highest_price: Optional[float] = None
    lowest_price: Optional[float] = None


class PortfolioTracker:
    """模拟组合管理器。

    功能：
    - 模拟开仓/平仓
    - 每根 bar 检查 SL/TP 触发
    - breakeven 止损（复用实盘 position_rules.check_breakeven）
    - trailing stop（复用实盘 position_rules.check_trailing_stop）
    - 日终自动平仓（复用实盘 position_rules.should_close_end_of_day）
    - 追踪资金曲线
    - 记录所有已关闭交易
    """

    def __init__(
        self,
        initial_balance: float,
        max_positions: int = 3,
        commission_per_lot: float = 0.0,
        slippage_points: float = 0.0,
        contract_size: float = 100.0,
        # breakeven / trailing（与实盘 PositionManager 相同参数）
        trailing_atr_multiplier: float = 1.0,
        breakeven_atr_threshold: float = 1.0,
        # 日终平仓（与实盘 PositionManager 相同参数）
        end_of_day_close_enabled: bool = False,
        end_of_day_close_hour_utc: int = 21,
        end_of_day_close_minute_utc: int = 0,
    ) -> None:
        self.initial_balance = initial_balance
        self.current_balance = initial_balance
        self.peak_balance = initial_balance
        self._max_positions = max_positions
        self._commission_per_lot = commission_per_lot
        self._slippage_points = slippage_points
        self._contract_size = contract_size
        # breakeven / trailing 配置
        self._trailing_atr_multiplier = trailing_atr_multiplier
        self._breakeven_atr_threshold = breakeven_atr_threshold
        # 日终平仓配置
        self._end_of_day_close_enabled = end_of_day_close_enabled
        self._end_of_day_close_hour_utc = end_of_day_close_hour_utc
        self._end_of_day_close_minute_utc = end_of_day_close_minute_utc
        self._last_end_of_day_close_date: Optional[str] = None

        self._open_positions: List[_Position] = []
        self._closed_trades: List[TradeRecord] = []
        self._equity_curve: List[Tuple[datetime, float]] = []
        self._current_bar_index: int = 0

    @property
    def closed_trades(self) -> List[TradeRecord]:
        return list(self._closed_trades)

    @property
    def equity_curve(self) -> List[Tuple[datetime, float]]:
        return list(self._equity_curve)

    @property
    def equity_values(self) -> List[float]:
        """纯数值资金曲线（用于统计计算）。"""
        return [v for _, v in self._equity_curve]

    @property
    def open_position_count(self) -> int:
        return len(self._open_positions)

    def record_equity(self, bar: OHLC) -> None:
        """记录当前资金快照（含浮动盈亏）。"""
        floating_pnl = self._floating_pnl(bar.close)
        equity = self.current_balance + floating_pnl
        self._equity_curve.append((bar.time, equity))

    def open_position(
        self,
        strategy: str,
        action: str,
        bar: OHLC,
        trade_params: TradeParameters,
        regime: str,
        confidence: float,
        bar_index: int,
        atr_at_entry: float = 0.0,
    ) -> bool:
        """尝试开仓。

        Returns:
            是否成功开仓。
        """
        if len(self._open_positions) >= self._max_positions:
            return False

        # 模拟滑点
        if action == "buy":
            entry_price = bar.close + self._slippage_points
        else:
            entry_price = bar.close - self._slippage_points

        # 扣除手续费
        commission = self._commission_per_lot * trade_params.position_size
        self.current_balance -= commission

        signal_id = f"bt_{uuid.uuid4().hex[:8]}"
        pos = _Position(
            signal_id=signal_id,
            strategy=strategy,
            action=action,
            entry_time=bar.time,
            entry_price=entry_price,
            stop_loss=trade_params.stop_loss,
            take_profit=trade_params.take_profit,
            position_size=trade_params.position_size,
            regime=regime,
            confidence=confidence,
            entry_bar_index=bar_index,
            atr_at_entry=atr_at_entry,
            highest_price=entry_price if action == "buy" else None,
            lowest_price=entry_price if action == "sell" else None,
        )
        self._open_positions.append(pos)
        return True

    def check_exits(self, bar: OHLC, bar_index: int) -> List[TradeRecord]:
        """检查所有持仓的 SL/TP 是否触发。

        使用 bar 的 high/low 判断触发，先检查 SL 再检查 TP（保守估计）。
        同时复用实盘 breakeven/trailing 逻辑更新 SL。
        """
        # 先检查日终平仓
        if self._end_of_day_close_enabled:
            eod = should_close_end_of_day(
                current_time=bar.time,
                close_hour_utc=self._end_of_day_close_hour_utc,
                close_minute_utc=self._end_of_day_close_minute_utc,
                last_close_date=self._last_end_of_day_close_date,
            )
            if eod.should_close and self._open_positions:
                self._last_end_of_day_close_date = bar.time.date().isoformat()
                return self._close_all_with_reason(bar, bar_index, "end_of_day")

        closed: List[TradeRecord] = []
        remaining: List[_Position] = []

        for pos in self._open_positions:
            # 更新极值价格（与实盘 PositionManager.update_price 一致）
            if pos.action == "buy":
                if pos.highest_price is None or bar.high > pos.highest_price:
                    pos.highest_price = bar.high
            elif pos.action == "sell":
                if pos.lowest_price is None or bar.low < pos.lowest_price:
                    pos.lowest_price = bar.low

            # 复用实盘 breakeven 判定逻辑
            if pos.atr_at_entry > 0:
                be_result = check_breakeven(
                    action=pos.action,
                    entry_price=pos.entry_price,
                    current_price=bar.close,
                    atr_at_entry=pos.atr_at_entry,
                    breakeven_atr_threshold=self._breakeven_atr_threshold,
                    already_applied=pos.breakeven_applied,
                )
                if be_result.should_apply and be_result.new_stop_loss is not None:
                    pos.stop_loss = be_result.new_stop_loss
                    pos.breakeven_applied = True

                # 复用实盘 trailing stop 判定逻辑
                trail_result = check_trailing_stop(
                    action=pos.action,
                    current_stop_loss=pos.stop_loss,
                    atr_at_entry=pos.atr_at_entry,
                    trailing_atr_multiplier=self._trailing_atr_multiplier,
                    breakeven_applied=pos.breakeven_applied,
                    highest_price=pos.highest_price,
                    lowest_price=pos.lowest_price,
                )
                if trail_result.should_update and trail_result.new_stop_loss is not None:
                    pos.stop_loss = trail_result.new_stop_loss
                    pos.trailing_active = True

            # SL/TP 触发检查
            exit_price: Optional[float] = None
            exit_reason: Optional[str] = None

            if pos.action == "buy":
                # 多头：low 触及 SL 或 high 触及 TP
                if bar.low <= pos.stop_loss:
                    exit_price = pos.stop_loss
                    exit_reason = "stop_loss"
                elif bar.high >= pos.take_profit:
                    exit_price = pos.take_profit
                    exit_reason = "take_profit"
            else:
                # 空头：high 触及 SL 或 low 触及 TP
                if bar.high >= pos.stop_loss:
                    exit_price = pos.stop_loss
                    exit_reason = "stop_loss"
                elif bar.low <= pos.take_profit:
                    exit_price = pos.take_profit
                    exit_reason = "take_profit"

            if exit_price is not None and exit_reason is not None:
                trade = self._close_position(pos, exit_price, bar.time, bar_index, exit_reason)
                closed.append(trade)
            else:
                remaining.append(pos)

        self._open_positions = remaining
        return closed

    def close_by_signal(
        self,
        strategy: str,
        bar: OHLC,
        bar_index: int,
    ) -> List[TradeRecord]:
        """按策略名关闭对应的持仓（信号退出）。"""
        closed: List[TradeRecord] = []
        remaining: List[_Position] = []

        for pos in self._open_positions:
            if pos.strategy == strategy:
                trade = self._close_position(
                    pos, bar.close, bar.time, bar_index, "signal_exit"
                )
                closed.append(trade)
            else:
                remaining.append(pos)

        self._open_positions = remaining
        return closed

    def close_all(self, bar: OHLC, bar_index: int) -> List[TradeRecord]:
        """强制平仓所有持仓。"""
        return self._close_all_with_reason(bar, bar_index, "end_of_test")

    def _close_all_with_reason(
        self, bar: OHLC, bar_index: int, reason: str
    ) -> List[TradeRecord]:
        """按指定原因平仓所有持仓。"""
        closed: List[TradeRecord] = []
        for pos in self._open_positions:
            trade = self._close_position(
                pos, bar.close, bar.time, bar_index, reason
            )
            closed.append(trade)
        self._open_positions = []
        return closed

    def _close_position(
        self,
        pos: _Position,
        exit_price: float,
        exit_time: datetime,
        bar_index: int,
        exit_reason: str,
    ) -> TradeRecord:
        """关闭持仓并计算 PnL。"""
        # 出场滑点模拟（与开仓方向相反）
        if pos.action == "buy":
            actual_exit = exit_price - self._slippage_points
        else:
            actual_exit = exit_price + self._slippage_points

        if pos.action == "buy":
            price_diff = actual_exit - pos.entry_price
        else:
            price_diff = pos.entry_price - actual_exit

        pnl = price_diff * pos.position_size * self._contract_size
        # 扣除出场手续费
        commission = self._commission_per_lot * pos.position_size
        pnl -= commission

        # 使用初始资金作为分母，避免资金耗尽时结果失真
        pnl_pct = (pnl / self.initial_balance * 100.0) if self.initial_balance > 0 else 0.0

        self.current_balance += pnl
        if self.current_balance > self.peak_balance:
            self.peak_balance = self.current_balance

        bars_held = bar_index - pos.entry_bar_index

        trade = TradeRecord(
            signal_id=pos.signal_id,
            strategy=pos.strategy,
            action=pos.action,
            entry_time=pos.entry_time,
            entry_price=pos.entry_price,
            exit_time=exit_time,
            exit_price=actual_exit,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            position_size=pos.position_size,
            pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct, 4),
            bars_held=bars_held,
            regime=pos.regime,
            confidence=pos.confidence,
            exit_reason=exit_reason,
        )
        self._closed_trades.append(trade)
        return trade

    def _floating_pnl(self, current_price: float) -> float:
        """计算所有持仓的浮动盈亏。"""
        total = 0.0
        for pos in self._open_positions:
            if pos.action == "buy":
                diff = current_price - pos.entry_price
            else:
                diff = pos.entry_price - current_price
            total += diff * pos.position_size * self._contract_size
        return total
