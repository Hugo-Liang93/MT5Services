"""Paper Trading 实时持仓管理器。

与回测 PortfolioTracker 不同，本模块适配实盘报价（bid/ask）驱动持仓退出，
而非 bar-by-bar 回放。复用生产环境的 SL/TP 计算和仓位大小公式。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from src.trading.execution.sizing import (
    RegimeSizing,
    TradeParameters,
    compute_trade_params,
    extract_atr_from_indicators,
)
from src.utils.timezone import utc_now

from .config import PaperTradingConfig
from .models import PaperMetrics, PaperTradeRecord

logger = logging.getLogger(__name__)


class PaperPortfolio:
    """实时模拟持仓管理器。

    线程安全性：调用方（PaperTradingBridge）负责加锁。
    本类自身不持有锁，保持纯逻辑。
    """

    def __init__(self, config: PaperTradingConfig, session_id: str) -> None:
        self._config = config
        self._session_id = session_id
        self._balance = config.initial_balance
        self._peak_balance = config.initial_balance
        self._max_drawdown_pct = 0.0
        self._open_positions: Dict[str, PaperTradeRecord] = {}
        self._closed_trades: List[PaperTradeRecord] = []
        self._equity_snapshots: List[Tuple[datetime, float]] = []

    @property
    def current_balance(self) -> float:
        return self._balance

    @property
    def open_position_count(self) -> int:
        return len(self._open_positions)

    @property
    def closed_trades(self) -> List[PaperTradeRecord]:
        return list(self._closed_trades)

    def open_position(
        self,
        *,
        strategy: str,
        direction: str,
        symbol: str,
        timeframe: str,
        entry_price: float,
        atr_value: float,
        confidence: float,
        regime: str,
        signal_id: str = "",
        indicators: Optional[Dict[str, Any]] = None,
    ) -> Optional[PaperTradeRecord]:
        """尝试开仓。返回开仓记录，若被拒绝返回 None。"""
        if len(self._open_positions) >= self._config.max_positions:
            return None

        cfg = self._config
        regime_sizing = RegimeSizing(
            tp_trending=cfg.regime_tp_trending,
            tp_ranging=cfg.regime_tp_ranging,
            tp_breakout=cfg.regime_tp_breakout,
            tp_uncertain=cfg.regime_tp_uncertain,
            sl_trending=cfg.regime_sl_trending,
            sl_ranging=cfg.regime_sl_ranging,
            sl_breakout=cfg.regime_sl_breakout,
            sl_uncertain=cfg.regime_sl_uncertain,
        )

        trade_params = compute_trade_params(
            action=direction,
            current_price=entry_price,
            atr_value=atr_value,
            account_balance=self._balance,
            timeframe=timeframe,
            risk_percent=cfg.risk_percent,
            contract_size=cfg.contract_size,
            regime=regime,
            regime_sizing=regime_sizing,
        )

        # 模拟滑点
        slippage = cfg.slippage_points * 0.01  # points → price
        if direction == "buy":
            actual_entry = entry_price + slippage
        else:
            actual_entry = entry_price - slippage

        trade_id = f"pt_{uuid4().hex[:12]}"
        now = utc_now()

        record = PaperTradeRecord(
            trade_id=trade_id,
            session_id=self._session_id,
            strategy=strategy,
            direction=direction,
            symbol=symbol,
            timeframe=timeframe,
            entry_time=now,
            entry_price=actual_entry,
            stop_loss=trade_params.stop_loss,
            take_profit=trade_params.take_profit,
            position_size=trade_params.position_size,
            confidence=confidence,
            regime=regime,
            signal_id=signal_id,
            current_sl=trade_params.stop_loss,
            current_tp=trade_params.take_profit,
            slippage_cost=abs(actual_entry - entry_price) * trade_params.position_size * cfg.contract_size,
            commission_cost=cfg.commission_per_lot * trade_params.position_size,
        )

        self._open_positions[trade_id] = record
        return record

    def restore_open_trade(self, record: PaperTradeRecord) -> None:
        """从 DB 恢复一个 open trade 到 portfolio（进程重启 recovery 用）。

        不做任何仓位限制/风险校验，信任 DB 数据来自之前同一 session 的合法状态。
        仅在启动阶段调用，保持与 open_position() 的仓位规则分离。
        """
        self._open_positions[record.trade_id] = record

    def restore_baseline(
        self, *, balance: float, closed_trades: Optional[List[PaperTradeRecord]] = None
    ) -> None:
        """从 DB 恢复账户基线（balance / peak / 已 closed trades）。

        进程重启 recovery 用：用 session.initial_balance + total_pnl 作为 balance
        近似（floating pnl 会在 _check_all_positions 第一次循环时重新计算）。
        closed_trades 可选；不恢复也不影响 open position 监控，但 metrics 会失去历史。
        """
        self._balance = float(balance)
        if self._balance > self._peak_balance:
            self._peak_balance = self._balance
        if closed_trades:
            self._closed_trades.extend(closed_trades)

    def check_exits(
        self,
        bid: float,
        ask: float,
        current_time: Optional[datetime] = None,
    ) -> List[PaperTradeRecord]:
        """检查所有持仓的退出条件，返回本次平仓的交易列表。"""
        now = current_time or utc_now()
        closed: List[PaperTradeRecord] = []

        for trade_id in list(self._open_positions):
            trade = self._open_positions[trade_id]
            exit_price, exit_reason = self._evaluate_exit(trade, bid, ask, now)
            if exit_price is not None and exit_reason is not None:
                self._close_position(trade, exit_price, exit_reason, now)
                closed.append(trade)

        return closed

    def floating_pnl(self, bid: float, ask: float) -> float:
        """计算所有持仓的浮动盈亏。"""
        total = 0.0
        for trade in self._open_positions.values():
            price = bid if trade.direction == "buy" else ask
            pnl = self._calc_pnl(trade, price)
            total += pnl
        return total

    def equity(self, bid: float, ask: float) -> float:
        """当前权益 = 余额 + 浮动盈亏。"""
        return self._balance + self.floating_pnl(bid, ask)

    def snapshot_equity(self, bid: float, ask: float) -> None:
        """记录权益快照，用于回撤计算。"""
        eq = self.equity(bid, ask)
        self._equity_snapshots.append((utc_now(), eq))
        if eq > self._peak_balance:
            self._peak_balance = eq
        if self._peak_balance > 0:
            dd = (self._peak_balance - eq) / self._peak_balance
            if dd > self._max_drawdown_pct:
                self._max_drawdown_pct = dd

    def compute_metrics(self) -> PaperMetrics:
        """基于已关闭交易计算绩效指标。"""
        trades = self._closed_trades
        if not trades:
            return PaperMetrics()

        wins = [t for t in trades if (t.pnl or 0) > 0]
        losses = [t for t in trades if (t.pnl or 0) <= 0]
        total_pnl = sum(t.pnl or 0 for t in trades)
        gross_profit = sum(t.pnl or 0 for t in wins) if wins else 0.0
        gross_loss = abs(sum(t.pnl or 0 for t in losses)) if losses else 0.0

        by_strategy: Dict[str, Dict[str, Any]] = {}
        by_regime: Dict[str, Dict[str, Any]] = {}
        for t in trades:
            for key, bucket in [(t.strategy, by_strategy), (t.regime, by_regime)]:
                if key not in bucket:
                    bucket[key] = {"trades": 0, "wins": 0, "pnl": 0.0}
                bucket[key]["trades"] += 1
                if (t.pnl or 0) > 0:
                    bucket[key]["wins"] += 1
                bucket[key]["pnl"] += t.pnl or 0

        for bucket in [by_strategy, by_regime]:
            for stats in bucket.values():
                stats["pnl"] = round(stats["pnl"], 2)
                stats["win_rate"] = (
                    round(stats["wins"] / stats["trades"], 4)
                    if stats["trades"] > 0
                    else 0.0
                )

        return PaperMetrics(
            total_trades=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=len(wins) / len(trades) if trades else 0.0,
            total_pnl=total_pnl,
            avg_pnl=total_pnl / len(trades) if trades else 0.0,
            max_drawdown_pct=self._max_drawdown_pct,
            profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else 0.0,
            avg_bars_held=(
                sum(t.bars_held for t in trades) / len(trades) if trades else 0.0
            ),
            best_trade_pnl=max((t.pnl or 0) for t in trades),
            worst_trade_pnl=min((t.pnl or 0) for t in trades),
            by_strategy=by_strategy,
            by_regime=by_regime,
        )

    def force_close_all(self, bid: float, ask: float) -> List[PaperTradeRecord]:
        """强制平仓所有持仓（session 结束时调用）。"""
        closed: List[PaperTradeRecord] = []
        now = utc_now()
        for trade_id in list(self._open_positions):
            trade = self._open_positions[trade_id]
            price = bid if trade.direction == "buy" else ask
            self._close_position(trade, price, "session_stop", now)
            closed.append(trade)
        return closed

    # ── 内部方法 ──────────────────────────────────────────────────────

    def _evaluate_exit(
        self,
        trade: PaperTradeRecord,
        bid: float,
        ask: float,
        now: datetime,
    ) -> Tuple[Optional[float], Optional[str]]:
        """评估单笔持仓退出条件。返回 (exit_price, reason) 或 (None, None)。"""
        # 确定当前价格（buy 用 bid 平仓，sell 用 ask 平仓）
        current_price = bid if trade.direction == "buy" else ask
        sl = trade.current_sl or trade.stop_loss
        tp = trade.current_tp or trade.take_profit

        # 更新 MFE/MAE
        unrealized = self._calc_pnl_raw(trade, current_price)
        if unrealized > trade.max_favorable_excursion:
            trade.max_favorable_excursion = unrealized
        if unrealized < -trade.max_adverse_excursion:
            trade.max_adverse_excursion = abs(unrealized)

        # SL 触发
        if trade.direction == "buy" and current_price <= sl:
            return sl, "stop_loss"
        if trade.direction == "sell" and current_price >= sl:
            return sl, "stop_loss"

        # TP 触发
        if trade.direction == "buy" and current_price >= tp:
            return tp, "take_profit"
        if trade.direction == "sell" and current_price <= tp:
            return tp, "take_profit"

        # Breakeven 检查
        atr_estimate = abs(trade.take_profit - trade.entry_price) / 2.5  # 反推 ATR
        if not trade.breakeven_activated and atr_estimate > 0:
            threshold = self._config.breakeven_atr_threshold * atr_estimate
            if trade.direction == "buy" and current_price >= trade.entry_price + threshold:
                trade.current_sl = trade.entry_price
                trade.breakeven_activated = True
            elif trade.direction == "sell" and current_price <= trade.entry_price - threshold:
                trade.current_sl = trade.entry_price
                trade.breakeven_activated = True

        # Trailing SL 检查
        if trade.breakeven_activated and atr_estimate > 0:
            trail_dist = self._config.trailing_atr_multiplier * atr_estimate
            if trade.direction == "buy":
                new_sl = current_price - trail_dist
                if new_sl > (trade.current_sl or 0):
                    trade.current_sl = new_sl
            else:
                new_sl = current_price + trail_dist
                if trade.current_sl is None or new_sl < trade.current_sl:
                    trade.current_sl = new_sl

        # Trailing TP 检查
        if self._config.trailing_tp_enabled and atr_estimate > 0:
            activation = self._config.trailing_tp_activation_atr * atr_estimate
            if trade.direction == "buy" and current_price >= trade.entry_price + activation:
                if not trade.trailing_activated:
                    trade.trailing_activated = True
                trail = self._config.trailing_tp_trail_atr * atr_estimate
                trail_exit = current_price - trail
                if trade.trailing_activated and unrealized > 0:
                    # 从最高点回撤超过 trail 距离则止盈
                    peak_price = trade.entry_price + trade.max_favorable_excursion / (
                        trade.position_size * self._config.contract_size
                    ) if trade.position_size > 0 else current_price
                    if current_price <= peak_price - trail:
                        return current_price, "trailing_tp"
            elif trade.direction == "sell" and current_price <= trade.entry_price - activation:
                if not trade.trailing_activated:
                    trade.trailing_activated = True
                trail = self._config.trailing_tp_trail_atr * atr_estimate
                if trade.trailing_activated and unrealized > 0:
                    trough_price = trade.entry_price - trade.max_favorable_excursion / (
                        trade.position_size * self._config.contract_size
                    ) if trade.position_size > 0 else current_price
                    if current_price >= trough_price + trail:
                        return current_price, "trailing_tp"

        # 日终平仓
        if self._config.end_of_day_close_enabled:
            if now.hour >= self._config.end_of_day_close_hour_utc:
                return current_price, "end_of_day"

        return None, None

    def _close_position(
        self,
        trade: PaperTradeRecord,
        exit_price: float,
        reason: str,
        now: datetime,
    ) -> None:
        """平仓并移入已关闭列表。"""
        trade.exit_time = now
        trade.exit_price = exit_price
        trade.exit_reason = reason
        trade.pnl = self._calc_pnl(trade, exit_price)
        if trade.entry_price != 0:
            trade.pnl_pct = trade.pnl / (
                trade.entry_price * trade.position_size * self._config.contract_size
            ) if trade.position_size > 0 else 0.0

        self._balance += trade.pnl
        self._closed_trades.append(trade)
        self._open_positions.pop(trade.trade_id, None)

    def _calc_pnl(self, trade: PaperTradeRecord, exit_price: float) -> float:
        """计算含成本的 PnL。"""
        raw = self._calc_pnl_raw(trade, exit_price)
        return raw - trade.commission_cost

    def _calc_pnl_raw(self, trade: PaperTradeRecord, price: float) -> float:
        """计算原始 PnL（不含成本）。"""
        size = trade.position_size * self._config.contract_size
        if trade.direction == "buy":
            return (price - trade.entry_price) * size
        else:
            return (trade.entry_price - price) * size
