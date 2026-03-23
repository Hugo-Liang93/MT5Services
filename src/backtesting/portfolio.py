"""组合跟踪器：模拟持仓管理、SL/TP 检测、资金曲线追踪。"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.clients.mt5_market import OHLC
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


class PortfolioTracker:
    """模拟组合管理器。

    功能：
    - 模拟开仓/平仓
    - 每根 bar 检查 SL/TP 触发
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
    ) -> None:
        self.initial_balance = initial_balance
        self.current_balance = initial_balance
        self.peak_balance = initial_balance
        self._max_positions = max_positions
        self._commission_per_lot = commission_per_lot
        self._slippage_points = slippage_points
        self._contract_size = contract_size
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
        )
        self._open_positions.append(pos)
        return True

    def check_exits(self, bar: OHLC, bar_index: int) -> List[TradeRecord]:
        """检查所有持仓的 SL/TP 是否触发。

        使用 bar 的 high/low 判断触发，先检查 SL 再检查 TP（保守估计）。
        """
        closed: List[TradeRecord] = []
        remaining: List[_Position] = []

        for pos in self._open_positions:
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
        closed: List[TradeRecord] = []
        for pos in self._open_positions:
            trade = self._close_position(
                pos, bar.close, bar.time, bar_index, "end_of_test"
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
        if pos.action == "buy":
            price_diff = exit_price - pos.entry_price
        else:
            price_diff = pos.entry_price - exit_price

        pnl = price_diff * pos.position_size * self._contract_size
        # 扣除出场手续费
        commission = self._commission_per_lot * pos.position_size
        pnl -= commission

        pnl_pct = (pnl / self.current_balance * 100.0) if self.current_balance > 0 else 0.0

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
            exit_price=exit_price,
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
