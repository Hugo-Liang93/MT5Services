"""Nightly WF 契约定义。

## 模块职责

纯数据契约（dataclass DTO）。不包含任何执行逻辑、IO、运行时依赖。
规则（feedback_no_compat_patches.md）：
  - 字段必填、类型严格
  - 无 `Optional[T] = None` 作为"兼容占位"：None 只用于语义明确的"未知/缺失"
  - 序列化通过 `to_dict()` 显式方法（不用 dataclass 默认转 dict）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class NightlyWFConfig:
    """Nightly WF 运行参数契约。

    Attributes:
        symbol: 交易品种（当前仅 XAUUSD）
        timeframes: 要评估的 TF 列表（M15/M30/H1 典型）
        start_date: 回测起始（ISO 字符串）
        end_date: 回测结束
        strategies: 目标策略名列表。空列表 = 全部已注册 + live-executable 策略
        sharpe_regression_threshold: Sharpe 相对前次下降超过此比例 → 标记 regression
        dd_regression_threshold: max_dd 相对前次恶化超过此比例 → 标记 regression
        workers: 并行 worker 数（ProcessPool）
        output_dir: 结果落盘目录
    """

    symbol: str
    timeframes: Tuple[str, ...]
    start_date: str
    end_date: str
    strategies: Tuple[str, ...]
    sharpe_regression_threshold: float
    dd_regression_threshold: float
    workers: int
    output_dir: str

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol required")
        if not self.timeframes:
            raise ValueError("timeframes must be non-empty")
        if self.workers < 1:
            raise ValueError(f"workers must be >= 1, got {self.workers}")
        if self.sharpe_regression_threshold <= 0:
            raise ValueError(
                f"sharpe_regression_threshold must be > 0, "
                f"got {self.sharpe_regression_threshold}"
            )
        if self.dd_regression_threshold <= 0:
            raise ValueError(
                f"dd_regression_threshold must be > 0, "
                f"got {self.dd_regression_threshold}"
            )


@dataclass(frozen=True)
class StrategyMetrics:
    """单个 (strategy, tf) 组合的回测指标。

    所有字段必填。缺失值由上游保证不会进入契约（失败的回测直接 raise）。
    """

    strategy: str
    timeframe: str
    trades: int
    win_rate: float       # [0, 1]
    pnl: float
    profit_factor: float
    sharpe: float
    sortino: float
    max_dd: float         # [0, 1]，负向指标越小越好
    expectancy: float
    avg_bars_held: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "timeframe": self.timeframe,
            "trades": self.trades,
            "win_rate": round(self.win_rate, 4),
            "pnl": round(self.pnl, 2),
            "profit_factor": round(self.profit_factor, 3),
            "sharpe": round(self.sharpe, 3),
            "sortino": round(self.sortino, 3),
            "max_dd": round(self.max_dd, 4),
            "expectancy": round(self.expectancy, 4),
            "avg_bars_held": round(self.avg_bars_held, 1),
        }


@dataclass(frozen=True)
class RegressionFinding:
    """回归检测结果：同一 (strategy, tf) 组合相比上次的恶化情况。"""

    strategy: str
    timeframe: str
    metric: str              # "sharpe" | "max_dd"
    previous: float
    current: float
    change_ratio: float      # (current - previous) / previous（负号 = 恶化）
    severity: str            # "regression" | "improvement" | "unchanged"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "timeframe": self.timeframe,
            "metric": self.metric,
            "previous": round(self.previous, 4),
            "current": round(self.current, 4),
            "change_ratio": round(self.change_ratio, 4),
            "severity": self.severity,
        }


@dataclass(frozen=True)
class NightlyWFReport:
    """Nightly WF 运行的完整报告。"""

    generated_at: datetime
    config: NightlyWFConfig
    metrics: Tuple[StrategyMetrics, ...]
    regressions: Tuple[RegressionFinding, ...] = field(default_factory=tuple)
    previous_report_path: Optional[str] = None
    runtime_seconds: float = 0.0
    failed_runs: Tuple[str, ...] = field(default_factory=tuple)

    def has_regression(self) -> bool:
        return any(r.severity == "regression" for r in self.regressions)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "symbol": self.config.symbol,
            "timeframes": list(self.config.timeframes),
            "start_date": self.config.start_date,
            "end_date": self.config.end_date,
            "strategies": list(self.config.strategies),
            "workers": self.config.workers,
            "runtime_seconds": round(self.runtime_seconds, 2),
            "metrics": [m.to_dict() for m in self.metrics],
            "regressions": [r.to_dict() for r in self.regressions],
            "failed_runs": list(self.failed_runs),
            "previous_report_path": self.previous_report_path,
            "has_regression": self.has_regression(),
        }
