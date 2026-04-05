"""Backtest analysis sub-package: metrics, Monte Carlo, and reporting."""

from .metrics import _empty_metrics, compute_metrics, compute_metrics_grouped
from .monte_carlo import run_monte_carlo
from .report import (
    format_optimization_summary,
    format_summary,
    format_timeframe_comparison,
    result_to_json,
)

__all__ = [
    "compute_metrics",
    "compute_metrics_grouped",
    "_empty_metrics",
    "run_monte_carlo",
    "format_summary",
    "format_optimization_summary",
    "format_timeframe_comparison",
    "result_to_json",
]
