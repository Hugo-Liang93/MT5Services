"""回测统计指标计算。"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple

from .models import BacktestMetrics, TradeRecord


def compute_metrics(
    trades: List[TradeRecord],
    initial_balance: float,
    equity_curve: List[float],
) -> BacktestMetrics:
    """从交易记录和资金曲线计算回测统计指标。

    Args:
        trades: 已关闭的交易记录
        initial_balance: 初始资金
        equity_curve: 资金曲线（纯数值序列）
    """
    if not trades:
        return _empty_metrics()

    winners = [t for t in trades if t.pnl > 0]
    losers = [t for t in trades if t.pnl <= 0]

    total_trades = len(trades)
    winning_trades = len(winners)
    losing_trades = len(losers)
    win_rate = winning_trades / total_trades if total_trades > 0 else 0.0

    total_profit = sum(t.pnl for t in winners)
    total_loss = abs(sum(t.pnl for t in losers))
    profit_factor = total_profit / total_loss if total_loss > 0 else 999.99

    avg_win = total_profit / winning_trades if winning_trades > 0 else 0.0
    avg_loss = total_loss / losing_trades if losing_trades > 0 else 0.0
    expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

    total_pnl = sum(t.pnl for t in trades)
    total_pnl_pct = (total_pnl / initial_balance) * 100.0 if initial_balance > 0 else 0.0

    avg_bars_held = sum(t.bars_held for t in trades) / total_trades

    # 从资金曲线计算收益率序列
    returns = _compute_returns(equity_curve)
    sharpe = _sharpe_ratio(returns)
    sortino = _sortino_ratio(returns)
    max_dd, max_dd_dur = _max_drawdown(equity_curve)

    # Calmar Ratio = 年化收益率 / 最大回撤
    calmar = 0.0
    if max_dd > 0 and initial_balance > 0 and len(equity_curve) >= 2:
        total_return_pct = total_pnl / initial_balance
        # 年化：假设 252 个交易日，基于 equity_curve 长度估算
        n_periods = len(equity_curve)
        annualization_factor = 252.0 / max(n_periods, 1)
        annualized_return = total_return_pct * annualization_factor
        calmar = annualized_return / max_dd

    return BacktestMetrics(
        total_trades=total_trades,
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        win_rate=round(win_rate, 4),
        expectancy=round(expectancy, 4),
        profit_factor=round(profit_factor, 4),
        sharpe_ratio=round(sharpe, 4),
        sortino_ratio=round(sortino, 4),
        max_drawdown=round(max_dd, 4),
        max_drawdown_duration=max_dd_dur,
        avg_win=round(avg_win, 4),
        avg_loss=round(avg_loss, 4),
        avg_bars_held=round(avg_bars_held, 2),
        total_pnl=round(total_pnl, 2),
        total_pnl_pct=round(total_pnl_pct, 4),
        calmar_ratio=round(calmar, 4),
    )


def compute_metrics_grouped(
    trades: List[TradeRecord],
    initial_balance: float,
    equity_curve: List[float],
    group_key: str,
) -> Dict[str, BacktestMetrics]:
    """按指定键分组计算统计指标。

    Args:
        group_key: "regime" | "strategy" | "confidence_level"
    """
    groups: Dict[str, List[TradeRecord]] = {}
    for trade in trades:
        if group_key == "regime":
            key = trade.regime or "unknown"
        elif group_key == "strategy":
            key = trade.strategy
        elif group_key == "confidence_level":
            key = _confidence_level(trade.confidence)
        else:
            key = "all"
        groups.setdefault(key, []).append(trade)

    result: Dict[str, BacktestMetrics] = {}
    for key, group_trades in groups.items():
        # 为子组使用简化的资金曲线（基于各组交易的累计 PnL）
        sub_curve = _build_sub_equity_curve(group_trades, initial_balance)
        result[key] = compute_metrics(group_trades, initial_balance, sub_curve)
    return result


def _confidence_level(confidence: float) -> str:
    if confidence >= 0.8:
        return "high"
    elif confidence >= 0.6:
        return "medium"
    return "low"


def _compute_returns(equity_curve: List[float]) -> List[float]:
    """计算逐期收益率。"""
    if len(equity_curve) < 2:
        return []
    returns = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]
        if prev > 0:
            returns.append((equity_curve[i] - prev) / prev)
        else:
            returns.append(0.0)
    return returns


def _sharpe_ratio(returns: List[float], risk_free_rate: float = 0.0) -> float:
    """年化 Sharpe 比率（假设每笔交易间隔相同）。"""
    if len(returns) < 2:
        return 0.0
    excess = [r - risk_free_rate for r in returns]
    mean_r = sum(excess) / len(excess)
    variance = sum((r - mean_r) ** 2 for r in excess) / (len(excess) - 1)
    std_r = math.sqrt(variance) if variance > 0 else 0.0
    if std_r == 0:
        return 0.0
    # 年化因子：假设 252 个交易日
    annualization = math.sqrt(252)
    return (mean_r / std_r) * annualization


def _sortino_ratio(returns: List[float], risk_free_rate: float = 0.0) -> float:
    """年化 Sortino 比率（仅计算下行波动率）。"""
    if len(returns) < 2:
        return 0.0
    excess = [r - risk_free_rate for r in returns]
    mean_r = sum(excess) / len(excess)
    downside = [r for r in excess if r < 0]
    if not downside:
        return 999.99 if mean_r > 0 else 0.0
    downside_var = sum(r ** 2 for r in downside) / len(downside)
    downside_std = math.sqrt(downside_var) if downside_var > 0 else 0.0
    if downside_std == 0:
        return 0.0
    annualization = math.sqrt(252)
    return (mean_r / downside_std) * annualization


def _max_drawdown(equity_curve: List[float]) -> Tuple[float, int]:
    """计算最大回撤（百分比）和最大回撤持续 bar 数。"""
    if not equity_curve:
        return 0.0, 0

    peak = equity_curve[0]
    max_dd = 0.0
    max_dd_duration = 0
    current_dd_start = 0

    for i, value in enumerate(equity_curve):
        if value >= peak:
            peak = value
            current_dd_start = i
        else:
            dd = (peak - value) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
                max_dd_duration = i - current_dd_start

    return max_dd, max_dd_duration


def _build_sub_equity_curve(
    trades: List[TradeRecord], initial_balance: float
) -> List[float]:
    """为子组交易构建简化资金曲线。"""
    curve = [initial_balance]
    balance = initial_balance
    for trade in sorted(trades, key=lambda t: t.exit_time):
        balance += trade.pnl
        curve.append(balance)
    return curve


def _empty_metrics() -> BacktestMetrics:
    return BacktestMetrics(
        total_trades=0,
        winning_trades=0,
        losing_trades=0,
        win_rate=0.0,
        expectancy=0.0,
        profit_factor=0.0,
        sharpe_ratio=0.0,
        sortino_ratio=0.0,
        max_drawdown=0.0,
        max_drawdown_duration=0,
        avg_win=0.0,
        avg_loss=0.0,
        avg_bars_held=0.0,
        total_pnl=0.0,
        total_pnl_pct=0.0,
        calmar_ratio=0.0,
    )
