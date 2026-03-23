"""回测模块：离线策略参数优化与绩效评估。"""

from __future__ import annotations

from .component_factory import build_backtest_components
from .filters import BacktestFilterConfig, BacktestFilterSimulator, BacktestFilterStats
from .models import (
    BacktestConfig,
    BacktestMetrics,
    BacktestResult,
    ParameterSpace,
    SignalEvaluation,
    TradeRecord,
)

__all__ = [
    "BacktestConfig",
    "BacktestFilterConfig",
    "build_backtest_components",
    "BacktestFilterSimulator",
    "BacktestFilterStats",
    "BacktestMetrics",
    "BacktestResult",
    "ParameterSpace",
    "SignalEvaluation",
    "TradeRecord",
]
