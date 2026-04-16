"""Nightly WF 自动化：契约 + 运行器 + 对比器。"""

from .contracts import (
    NightlyWFConfig,
    NightlyWFReport,
    RegressionFinding,
    StrategyMetrics,
)
from .regression import compare_reports
from .runner import run_nightly_wf

__all__ = [
    "NightlyWFConfig",
    "NightlyWFReport",
    "RegressionFinding",
    "StrategyMetrics",
    "compare_reports",
    "run_nightly_wf",
]
