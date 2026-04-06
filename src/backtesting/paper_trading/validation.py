"""Paper Trading vs Backtest 验证对比。

纯函数实现：接收 PaperMetrics + BacktestMetrics，返回 ValidationVerdict。
不引入后台任务或额外服务组件。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class MetricComparison:
    """单个指标的对比结果。"""

    metric_name: str
    backtest_value: float
    paper_value: float
    deviation_pct: float  # (paper - backtest) / |backtest| * 100
    tolerance_pct: float
    within_tolerance: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric_name": self.metric_name,
            "backtest_value": round(self.backtest_value, 4),
            "paper_value": round(self.paper_value, 4),
            "deviation_pct": round(self.deviation_pct, 2),
            "tolerance_pct": round(self.tolerance_pct, 2),
            "within_tolerance": self.within_tolerance,
        }


@dataclass(frozen=True)
class ValidationVerdict:
    """Paper Trading vs Backtest 对比裁决。"""

    paper_session_id: str
    backtest_run_id: str
    experiment_id: Optional[str]

    comparisons: List[MetricComparison]
    overall_pass: bool
    trade_count_sufficient: bool
    summary: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "paper_session_id": self.paper_session_id,
            "backtest_run_id": self.backtest_run_id,
            "experiment_id": self.experiment_id,
            "comparisons": [c.to_dict() for c in self.comparisons],
            "overall_pass": self.overall_pass,
            "trade_count_sufficient": self.trade_count_sufficient,
            "summary": self.summary,
        }


def _compute_deviation_pct(paper: float, backtest: float) -> float:
    """计算偏差百分比，backtest 为 0 时特殊处理。"""
    if abs(backtest) < 1e-9:
        return 0.0 if abs(paper) < 1e-9 else 100.0
    return (paper - backtest) / abs(backtest) * 100.0


def _compare_metric(
    name: str,
    paper_value: float,
    backtest_value: float,
    tolerance_pct: float,
    *,
    higher_is_worse: bool = False,
) -> MetricComparison:
    """对比单个指标。

    Args:
        higher_is_worse: True 表示 paper 值越高越差（如 max_drawdown），
                         此时只检查上偏是否超限。
    """
    deviation = _compute_deviation_pct(paper_value, backtest_value)
    if higher_is_worse:
        within = deviation <= tolerance_pct
    else:
        within = abs(deviation) <= tolerance_pct
    return MetricComparison(
        metric_name=name,
        backtest_value=backtest_value,
        paper_value=paper_value,
        deviation_pct=deviation,
        tolerance_pct=tolerance_pct,
        within_tolerance=within,
    )


def compare_paper_vs_backtest(
    paper_metrics: Dict[str, Any],
    backtest_metrics: Dict[str, Any],
    *,
    min_paper_trades: int = 20,
    win_rate_tolerance_pct: float = 15.0,
    sharpe_tolerance_pct: float = 50.0,
    max_drawdown_tolerance_pct: float = 50.0,
    paper_session_id: str = "",
    backtest_run_id: str = "",
    experiment_id: Optional[str] = None,
) -> ValidationVerdict:
    """纯函数：对比 Paper Trading 指标 vs 回测指标。

    Args:
        paper_metrics: PaperMetrics.to_dict() 或等价字典
        backtest_metrics: BacktestMetrics 等价字典
            （至少含 win_rate, sharpe_ratio, max_drawdown, total_trades）
        min_paper_trades: Paper Trading 最少交易数才有对比意义
        win_rate_tolerance_pct: 胜率容差（±百分比）
        sharpe_tolerance_pct: Sharpe 容差（±百分比）
        max_drawdown_tolerance_pct: 最大回撤容差（Paper DD 可比 BT 高多少%）

    Returns:
        ValidationVerdict 裁决结果
    """
    paper_trades = paper_metrics.get("total_trades", 0)
    trade_count_sufficient = paper_trades >= min_paper_trades

    comparisons: List[MetricComparison] = []

    # 胜率对比
    paper_wr = paper_metrics.get("win_rate", 0.0)
    bt_wr = backtest_metrics.get("win_rate", 0.0)
    comparisons.append(
        _compare_metric("win_rate", paper_wr, bt_wr, win_rate_tolerance_pct)
    )

    # Sharpe 对比
    paper_sharpe = paper_metrics.get("sharpe_ratio", 0.0)
    if paper_sharpe is None:
        paper_sharpe = 0.0
    bt_sharpe = backtest_metrics.get("sharpe_ratio", 0.0)
    comparisons.append(
        _compare_metric("sharpe_ratio", paper_sharpe, bt_sharpe, sharpe_tolerance_pct)
    )

    # 最大回撤对比（higher_is_worse：Paper DD 比 BT 高才算差）
    paper_dd = paper_metrics.get("max_drawdown_pct", 0.0)
    bt_dd = backtest_metrics.get("max_drawdown", 0.0)
    comparisons.append(
        _compare_metric(
            "max_drawdown",
            paper_dd,
            bt_dd,
            max_drawdown_tolerance_pct,
            higher_is_worse=True,
        )
    )

    # 总体裁决：样本充足 + 所有对比指标在容差内
    all_within = all(c.within_tolerance for c in comparisons)
    overall_pass = trade_count_sufficient and all_within

    # 生成摘要
    parts: List[str] = []
    for c in comparisons:
        status = "PASS" if c.within_tolerance else "FAIL"
        parts.append(
            f"{c.metric_name}: BT={c.backtest_value:.4f} "
            f"Paper={c.paper_value:.4f} "
            f"dev={c.deviation_pct:+.1f}% [{status}]"
        )
    if not trade_count_sufficient:
        parts.append(
            f"trades: Paper={paper_trades} < min({min_paper_trades}) [INSUFFICIENT]"
        )
    else:
        parts.append(f"trades: Paper={paper_trades} >= min({min_paper_trades}) [OK]")
    verdict_str = "PASS" if overall_pass else "FAIL"
    summary = f"Verdict: {verdict_str}\n" + "\n".join(parts)

    return ValidationVerdict(
        paper_session_id=paper_session_id,
        backtest_run_id=backtest_run_id,
        experiment_id=experiment_id,
        comparisons=comparisons,
        overall_pass=overall_pass,
        trade_count_sufficient=trade_count_sufficient,
        summary=summary,
    )
