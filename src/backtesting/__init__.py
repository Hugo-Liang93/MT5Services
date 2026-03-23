"""回测模块：离线策略参数优化与绩效评估。"""

from __future__ import annotations

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
    "BacktestFilterSimulator",
    "BacktestFilterStats",
    "BacktestMetrics",
    "BacktestResult",
    "ParameterSpace",
    "SignalEvaluation",
    "TradeRecord",
]
