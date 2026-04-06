"""组合跟踪器：模拟持仓管理。

出场系统：统一出场规则（src/trading/positions/exit_rules.py）
  4 条独立规则：初始 SL / Chandelier trailing / 信号反转 / 超时
  回测和实盘共用同一套纯函数（evaluate_exit）。
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Deque, Dict, List, Optional, Tuple

from src.clients.mt5_market import OHLC
from src.trading.positions.exit_rules import (
    ChandelierConfig,
    evaluate_exit,
    check_end_of_day,
)
from src.trading.execution import TradeParameters

from ..models import TradeRecord

logger = logging.getLogger(__name__)


@dataclass
class _Position:
    """内部持仓表示。"""

    signal_id: str
    strategy: str
    direction: str  # "buy" | "sell"
    entry_time: datetime
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    regime: str
    confidence: float
    entry_bar_index: int  # 开仓时的 bar 索引（用于计算 bars_held）
    atr_at_entry: float = 0.0
    # Chandelier Exit 状态
    initial_risk: float = 0.0  # R = |entry - initial_sl|，开仓时计算，不变
    peak_price: Optional[float] = None  # 持仓期最高价（多头）/最低价（空头）
    bars_held: int = 0
    bars_since_peak: int = 0
    breakeven_activated: bool = False
    recent_signal_dirs: list = field(default_factory=list)  # 最近 N bar 策略方向
    strategy_category: str = ""  # trend / reversion / breakout
    timeframe: str = ""  # 用于 per-TF trail 缩放
    sl_atr_mult: float = 0.0  # 入场 SL 的 ATR 倍数（用于 R 单位保护）
    # 旧体系兼容字段
    breakeven_applied: bool = False
    trailing_active: bool = False
    highest_price: Optional[float] = None
    lowest_price: Optional[float] = None


class PortfolioTracker:
    """模拟组合管理器。

    功能：
    - 模拟开仓/平仓
    - 每根 bar 检查出场（Chandelier Exit 4 规则）
    - 日终自动平仓
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
        min_volume: float = 0.01,
        max_volume: float = 1.0,
        max_volume_per_order: Optional[float] = None,
        max_volume_per_symbol: Optional[float] = None,
        max_volume_per_day: Optional[float] = None,
        daily_loss_limit_pct: Optional[float] = None,
        max_trades_per_day: Optional[int] = None,
        max_trades_per_hour: Optional[int] = None,
        # Chandelier Exit 出场配置
        chandelier_config: Optional[ChandelierConfig] = None,
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
        self._min_volume = min_volume
        self._max_volume = max_volume
        self._max_volume_per_order = max_volume_per_order
        self._max_volume_per_symbol = max_volume_per_symbol
        self._max_volume_per_day = max_volume_per_day
        self._daily_loss_limit_pct = daily_loss_limit_pct
        self._max_trades_per_day = max_trades_per_day
        self._max_trades_per_hour = max_trades_per_hour
        # Chandelier Exit
        self._chandelier_config = chandelier_config or ChandelierConfig()
        # 日终平仓配置
        self._end_of_day_close_enabled = end_of_day_close_enabled
        self._end_of_day_close_hour_utc = end_of_day_close_hour_utc
        self._end_of_day_close_minute_utc = end_of_day_close_minute_utc
        self._last_end_of_day_close_date: Optional[str] = None

        self._open_positions: List[_Position] = []
        self._closed_trades: List[TradeRecord] = []
        self._equity_curve: List[Tuple[datetime, float]] = []
        self._current_bar_index: int = 0
        # O(1) 日内交易计数（key = date ISO string）
        self._daily_trade_counts: Dict[str, int] = {}
        # 滚动 1 小时交易时间戳窗口（deque 左侧自动淘汰，摊销 O(1)）
        self._hourly_window: Deque[datetime] = deque()
        self._daily_opened_volume: Dict[str, float] = {}
        self._daily_start_equity: Dict[str, float] = {}

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
        self._ensure_day_start_equity(bar.time, equity)
        self._equity_curve.append((bar.time, equity))

    def observe_bar(self, bar: OHLC) -> None:
        """记录日初权益基线，供日损限制使用。"""
        self._ensure_day_start_equity(
            bar.time,
            self.current_balance + self._floating_pnl(bar.open),
        )

    def can_open_position(
        self,
        bar: OHLC,
        trade_params: TradeParameters,
    ) -> tuple[bool, str]:
        """检查开仓是否满足回测风控限制。"""
        if len(self._open_positions) >= self._max_positions:
            return False, "max_positions"

        volume = float(trade_params.position_size)
        if volume < self._min_volume:
            return False, "min_volume"
        if volume > self._max_volume:
            return False, "max_volume"
        if self._max_volume_per_order is not None and volume > self._max_volume_per_order:
            return False, "max_volume_per_order"

        open_volume = sum(pos.position_size for pos in self._open_positions)
        if self._max_volume_per_symbol is not None and open_volume + volume > self._max_volume_per_symbol:
            return False, "max_volume_per_symbol"

        day_key = bar.time.date().isoformat()
        day_opened_volume = self._daily_opened_volume.get(day_key, 0.0)
        if self._max_volume_per_day is not None and day_opened_volume + volume > self._max_volume_per_day:
            return False, "max_volume_per_day"

        if self._max_trades_per_day is not None and self._max_trades_per_day > 0:
            trades_today = self._daily_trade_counts.get(day_key, 0)
            if trades_today >= self._max_trades_per_day:
                return False, "max_trades_per_day"

        if self._max_trades_per_hour is not None and self._max_trades_per_hour > 0:
            hour_ago = bar.time - timedelta(hours=1)
            # 修剪滑出 1 小时窗口的旧记录（摊销 O(1)）
            while self._hourly_window and self._hourly_window[0] < hour_ago:
                self._hourly_window.popleft()
            if len(self._hourly_window) >= self._max_trades_per_hour:
                return False, "max_trades_per_hour"

        # _floating_pnl 只计算一次，避免重复遍历所有持仓
        current_close_equity = self.current_balance + self._floating_pnl(bar.close)
        self._ensure_day_start_equity(bar.time, current_close_equity)
        start_equity = self._daily_start_equity.get(day_key)
        if (
            self._daily_loss_limit_pct is not None
            and start_equity is not None
            and start_equity > 0
        ):
            loss_pct = max(0.0, ((start_equity - current_close_equity) / start_equity) * 100.0)
            if loss_pct >= self._daily_loss_limit_pct:
                return False, "daily_loss_limit_pct"

        return True, ""

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
        strategy_category: str = "",
        timeframe: str = "",
    ) -> bool:
        """尝试开仓。

        Returns:
            是否成功开仓。
        """
        allowed, _ = self.can_open_position(bar, trade_params)
        if not allowed:
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
        initial_risk = abs(entry_price - trade_params.stop_loss)
        pos = _Position(
            signal_id=signal_id,
            strategy=strategy,
            direction=action,
            entry_time=bar.time,
            entry_price=entry_price,
            stop_loss=trade_params.stop_loss,
            take_profit=trade_params.take_profit,
            position_size=trade_params.position_size,
            regime=regime,
            confidence=confidence,
            entry_bar_index=bar_index,
            atr_at_entry=atr_at_entry,
            initial_risk=initial_risk,
            peak_price=entry_price,
            strategy_category=strategy_category,
            timeframe=timeframe,
            sl_atr_mult=round(trade_params.sl_distance / atr_at_entry, 4) if atr_at_entry > 0 else 0.0,
            highest_price=entry_price if action == "buy" else None,
            lowest_price=entry_price if action == "sell" else None,
        )
        self._open_positions.append(pos)
        day_key = bar.time.date().isoformat()
        self._daily_trade_counts[day_key] = self._daily_trade_counts.get(day_key, 0) + 1
        self._hourly_window.append(bar.time)
        self._daily_opened_volume[day_key] = self._daily_opened_volume.get(day_key, 0.0) + trade_params.position_size
        # 交易事件自动记录资金快照（精确捕捉开仓时刻的资金变化）
        self._equity_curve.append(
            (bar.time, self.current_balance + self._floating_pnl(bar.close))
        )
        return True

    def _ensure_day_start_equity(self, timestamp: datetime, equity: float) -> None:
        day_key = timestamp.date().isoformat()
        self._daily_start_equity.setdefault(day_key, equity)

    def check_exits(
        self,
        bar: OHLC,
        bar_index: int,
        indicators: Dict[str, Dict] = {},  # noqa: B006
        *,
        current_atr: float = 0.0,
        current_regime: str = "",
        signal_directions: Optional[Dict[str, str]] = None,
    ) -> List[TradeRecord]:
        """检查所有持仓是否应平仓。

        使用 Chandelier Exit 4 规则系统：
        1. 初始 SL 触发
        2. Chandelier trailing（current_ATR 动态）+ Breakeven 保护
        3. 信号反转 N-bar 确认
        4. 超时退出

        Args:
            bar: 当前 OHLC bar
            bar_index: bar 索引
            indicators: 指标数据（未使用，保留接口兼容）
            current_atr: 当前 ATR 值（Chandelier trailing 用）
            signal_directions: 当前 bar 各策略/投票组方向
                {"momentum_vote": "buy", "trend_triple_confirm": "sell", ...}
        """
        # 先检查日终平仓
        if self._end_of_day_close_enabled:
            eod_triggered = check_end_of_day(
                current_time=bar.time,
                close_hour_utc=self._end_of_day_close_hour_utc,
                close_minute_utc=self._end_of_day_close_minute_utc,
                last_close_date=self._last_end_of_day_close_date,
            )
            if eod_triggered and self._open_positions:
                self._last_end_of_day_close_date = bar.time.date().isoformat()
                return self._close_all_with_reason(bar, bar_index, "end_of_day")

        closed: List[TradeRecord] = []
        remaining: List[_Position] = []
        sig_dirs = signal_directions or {}

        for pos in self._open_positions:
            # 更新 peak price
            if pos.direction == "buy":
                old_peak = pos.peak_price or pos.entry_price
                new_peak = max(old_peak, bar.high)
                if new_peak > old_peak:
                    pos.peak_price = new_peak
                    pos.bars_since_peak = 0
                else:
                    pos.bars_since_peak += 1
                # 旧字段兼容
                pos.highest_price = pos.peak_price
            else:
                old_peak = pos.peak_price or pos.entry_price
                new_peak = min(old_peak, bar.low)
                if new_peak < old_peak:
                    pos.peak_price = new_peak
                    pos.bars_since_peak = 0
                else:
                    pos.bars_since_peak += 1
                pos.lowest_price = pos.peak_price

            pos.bars_held += 1

            # 记录策略方向到 recent_signal_dirs
            strategy_dir = sig_dirs.get(pos.strategy, "")
            if strategy_dir:
                pos.recent_signal_dirs.append(strategy_dir)
                # 只保留最近 N 条（N = confirmation_bars + 缓冲）
                max_keep = self._chandelier_config.signal_exit_confirmation_bars + 2
                if len(pos.recent_signal_dirs) > max_keep:
                    pos.recent_signal_dirs = pos.recent_signal_dirs[-max_keep:]

            # Chandelier Exit 统一评估（regime-aware + per-TF 缩放 + R 单位保护）
            result = evaluate_exit(
                action=pos.direction,
                entry_price=pos.entry_price,
                bar_high=bar.high,
                bar_low=bar.low,
                bar_close=bar.close,
                current_stop_loss=pos.stop_loss,
                initial_risk=pos.initial_risk,
                peak_price=pos.peak_price or pos.entry_price,
                current_atr=current_atr,
                bars_held=pos.bars_held,
                breakeven_already_activated=pos.breakeven_activated,
                recent_signal_dirs=pos.recent_signal_dirs,
                strategy_category=pos.strategy_category,
                current_regime=current_regime,
                timeframe=pos.timeframe,
                initial_sl_atr_mult=pos.sl_atr_mult,
                config=self._chandelier_config,
            )

            # 更新持仓状态
            pos.breakeven_activated = result.breakeven_activated
            if result.new_stop_loss is not None:
                pos.stop_loss = result.new_stop_loss

            if result.should_close:
                # 确定平仓价格
                if result.close_reason == "take_profit":
                    # TP 按硬上界价格成交
                    tp_dist = self._chandelier_config.max_tp_r * pos.initial_risk
                    if pos.direction == "buy":
                        exit_price = pos.entry_price + tp_dist
                    else:
                        exit_price = pos.entry_price - tp_dist
                elif result.close_reason in ("stop_loss", "trailing_stop"):
                    exit_price = pos.stop_loss
                else:
                    # signal_exit / timeout → 按 bar.close 平仓
                    exit_price = bar.close

                trade = self._close_position(
                    pos, exit_price, bar.time, bar_index, result.close_reason,
                )
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
        if pos.direction == "buy":
            actual_exit = exit_price - self._slippage_points
        else:
            actual_exit = exit_price + self._slippage_points

        if pos.direction == "buy":
            price_diff = actual_exit - pos.entry_price
        else:
            price_diff = pos.entry_price - actual_exit

        pnl = price_diff * pos.position_size * self._contract_size
        # 扣除出场手续费
        exit_commission = self._commission_per_lot * pos.position_size
        entry_commission = self._commission_per_lot * pos.position_size
        total_commission = entry_commission + exit_commission
        pnl -= exit_commission

        # 计算滑点成本（开仓+平仓双向滑点 × 合约规模）
        slippage_cost = (
            self._slippage_points * 2 * pos.position_size * self._contract_size
        )

        # 使用初始资金作为分母，避免资金耗尽时结果失真
        pnl_pct = (pnl / self.initial_balance * 100.0) if self.initial_balance > 0 else 0.0

        self.current_balance += pnl
        if self.current_balance > self.peak_balance:
            self.peak_balance = self.current_balance
        # 交易事件自动记录资金快照（精确捕捉平仓时刻的资金变化）
        self._equity_curve.append((exit_time, self.current_balance))

        bars_held = bar_index - pos.entry_bar_index

        trade = TradeRecord(
            signal_id=pos.signal_id,
            strategy=pos.strategy,
            direction=pos.direction,
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
            slippage_cost=round(slippage_cost, 2),
            commission_cost=round(total_commission, 2),
        )
        self._closed_trades.append(trade)
        return trade

    def _floating_pnl(self, current_price: float) -> float:
        """计算所有持仓的浮动盈亏。"""
        total = 0.0
        for pos in self._open_positions:
            if pos.direction == "buy":
                diff = current_price - pos.entry_price
            else:
                diff = pos.entry_price - current_price
            total += diff * pos.position_size * self._contract_size
        return total
