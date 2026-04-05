"""回测模块：离线策略参数优化与绩效评估。"""

from __future__ import annotations

from .component_factory import build_backtest_components
from .data import CachedDataLoader
from .engine import BacktestEngine, PortfolioTracker
from .filtering import BacktestFilterConfig, BacktestFilterSimulator, BacktestFilterStats
from .models import (
    BacktestConfig,
    BacktestMetrics,
    BacktestResult,
    ParamChange,
    ParameterSpace,
    Recommendation,
    RecommendationStatus,
    SignalEvaluation,
    TradeRecord,
)
from .optimization import ConfigApplicator, RecommendationEngine

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestFilterConfig",
    "build_backtest_components",
    "CachedDataLoader",
    "BacktestFilterSimulator",
    "BacktestFilterStats",
    "BacktestMetrics",
    "BacktestResult",
    "ConfigApplicator",
    "ParamChange",
    "PortfolioTracker",
    "ParameterSpace",
    "Recommendation",
    "RecommendationEngine",
    "RecommendationStatus",
    "SignalEvaluation",
    "TradeRecord",
]
