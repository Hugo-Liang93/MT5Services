"""蒙特卡洛排列检验 — 评估策略收益的统计显著性。

核心思想：随机打乱交易 PnL 序列 N 次，重算 Sharpe/PF/MaxDD，
如果真实策略的指标在随机分布中处于前 5%（p < 0.05），
说明策略收益不太可能是运气。

设计原则：
- 纯函数，不依赖任何运行时组件
- 可被 BacktestEngine、optimizer、walk_forward 直接调用
- 配置化：所有参数通过 MonteCarloConfig 传入
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class MonteCarloConfig:
    """蒙特卡洛排列检验配置。"""

    enabled: bool = False
    num_simulations: int = 1000
    confidence_level: float = 0.95
    seed: Optional[int] = None


@dataclass(frozen=True)
class MonteCarloResult:
    """蒙特卡洛排列检验结果。"""

    num_simulations: int
    num_trades: int

    # 真实策略指标
    real_sharpe: float
    real_profit_factor: float
    real_max_drawdown: float
    real_total_pnl: float

    # p-value（越小越显著）
    sharpe_p_value: float
    profit_factor_p_value: float
    max_drawdown_p_value: float

    # 随机分布的百分位
    sharpe_percentile: float
    profit_factor_percentile: float

    # 随机分布统计
    random_sharpe_mean: float
    random_sharpe_std: float
    random_sharpe_95th: float

    # 显著性判定
    is_significant: bool

    def to_dict(self) -> dict:
        return {
            "num_simulations": self.num_simulations,
            "num_trades": self.num_trades,
            "real_sharpe": round(self.real_sharpe, 4),
            "real_profit_factor": round(self.real_profit_factor, 4),
            "real_max_drawdown": round(self.real_max_drawdown, 4),
            "real_total_pnl": round(self.real_total_pnl, 2),
            "sharpe_p_value": round(self.sharpe_p_value, 4),
            "profit_factor_p_value": round(self.profit_factor_p_value, 4),
            "max_drawdown_p_value": round(self.max_drawdown_p_value, 4),
            "sharpe_percentile": round(self.sharpe_percentile, 2),
            "profit_factor_percentile": round(self.profit_factor_percentile, 2),
            "random_sharpe_mean": round(self.random_sharpe_mean, 4),
            "random_sharpe_std": round(self.random_sharpe_std, 4),
            "random_sharpe_95th": round(self.random_sharpe_95th, 4),
            "is_significant": self.is_significant,
        }


def run_monte_carlo(
    pnl_sequence: List[float],
    initial_balance: float,
    config: Optional[MonteCarloConfig] = None,
) -> MonteCarloResult:
    """对交易 PnL 序列进行蒙特卡洛排列检验。

    Args:
        pnl_sequence: 每笔交易的盈亏金额（按时间顺序）
        initial_balance: 初始资金
        config: 检验配置

    Returns:
        MonteCarloResult 包含 p-value 和显著性判定
    """
    cfg = config or MonteCarloConfig()
    n = len(pnl_sequence)

    if n < 10:
        return _insufficient_data(n, pnl_sequence, initial_balance, cfg)

    rng = random.Random(cfg.seed)

    # 真实策略指标
    real_sharpe = _sharpe_from_pnl(pnl_sequence, initial_balance)
    real_pf = _profit_factor(pnl_sequence)
    real_dd = _max_drawdown_from_pnl(pnl_sequence, initial_balance)
    real_total = sum(pnl_sequence)

    # 蒙特卡洛模拟：随机翻转每笔交易的方向（sign randomization）
    # 如果策略的方向预测有价值，真实 PnL 的均值应显著优于随机翻转
    abs_pnl = [abs(p) for p in pnl_sequence]
    random_sharpes: List[float] = []
    random_pfs: List[float] = []
    random_dds: List[float] = []

    for _ in range(cfg.num_simulations):
        randomized = [v * rng.choice((-1, 1)) for v in abs_pnl]
        random_sharpes.append(_sharpe_from_pnl(randomized, initial_balance))
        random_pfs.append(_profit_factor(randomized))
        random_dds.append(_max_drawdown_from_pnl(randomized, initial_balance))

    # p-value: 随机中 >= 真实的比例（单尾）
    sharpe_p = sum(1 for s in random_sharpes if s >= real_sharpe) / cfg.num_simulations
    pf_p = sum(1 for p in random_pfs if p >= real_pf) / cfg.num_simulations
    # MaxDD: 随机中 <= 真实的比例（DD 越小越好）
    dd_p = sum(1 for d in random_dds if d <= real_dd) / cfg.num_simulations

    # 百分位
    sharpe_pct = sum(1 for s in random_sharpes if s < real_sharpe) / cfg.num_simulations * 100
    pf_pct = sum(1 for p in random_pfs if p < real_pf) / cfg.num_simulations * 100

    # 随机分布统计
    rs_mean = sum(random_sharpes) / len(random_sharpes)
    rs_std = _std(random_sharpes, rs_mean)
    sorted_sharpes = sorted(random_sharpes)
    rs_95th = sorted_sharpes[int(len(sorted_sharpes) * 0.95)]

    is_sig = sharpe_p < (1.0 - cfg.confidence_level)

    return MonteCarloResult(
        num_simulations=cfg.num_simulations,
        num_trades=n,
        real_sharpe=real_sharpe,
        real_profit_factor=real_pf,
        real_max_drawdown=real_dd,
        real_total_pnl=real_total,
        sharpe_p_value=sharpe_p,
        profit_factor_p_value=pf_p,
        max_drawdown_p_value=dd_p,
        sharpe_percentile=sharpe_pct,
        profit_factor_percentile=pf_pct,
        random_sharpe_mean=rs_mean,
        random_sharpe_std=rs_std,
        random_sharpe_95th=rs_95th,
        is_significant=is_sig,
    )


# ── 内部计算函数 ────────────────────────────────────────────────


def _sharpe_from_pnl(pnl_list: List[float], initial_balance: float) -> float:
    """从 PnL 序列计算 Sharpe Ratio（年化，假设日频）。"""
    if len(pnl_list) < 2 or initial_balance <= 0:
        return 0.0
    returns = [p / initial_balance for p in pnl_list]
    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std_r = math.sqrt(variance) if variance > 0 else 0.0
    if std_r < 1e-12:
        return 0.0
    return (mean_r / std_r) * math.sqrt(252)


def _profit_factor(pnl_list: List[float]) -> float:
    """计算 Profit Factor。"""
    gross_profit = sum(p for p in pnl_list if p > 0)
    gross_loss = abs(sum(p for p in pnl_list if p < 0))
    if gross_loss < 1e-12:
        return 99.99 if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _max_drawdown_from_pnl(pnl_list: List[float], initial_balance: float) -> float:
    """从 PnL 序列计算最大回撤（百分比）。"""
    if not pnl_list:
        return 0.0
    equity = initial_balance
    peak = equity
    max_dd = 0.0
    for pnl in pnl_list:
        equity += pnl
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _std(values: List[float], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _insufficient_data(
    n: int,
    pnl_sequence: List[float],
    initial_balance: float,
    cfg: MonteCarloConfig,
) -> MonteCarloResult:
    """样本不足时返回无效结果。"""
    real_total = sum(pnl_sequence) if pnl_sequence else 0.0
    return MonteCarloResult(
        num_simulations=0,
        num_trades=n,
        real_sharpe=0.0,
        real_profit_factor=0.0,
        real_max_drawdown=0.0,
        real_total_pnl=real_total,
        sharpe_p_value=1.0,
        profit_factor_p_value=1.0,
        max_drawdown_p_value=1.0,
        sharpe_percentile=0.0,
        profit_factor_percentile=0.0,
        random_sharpe_mean=0.0,
        random_sharpe_std=0.0,
        random_sharpe_95th=0.0,
        is_significant=False,
    )
