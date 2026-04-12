"""回测模块：离线策略参数优化与绩效评估。"""

from __future__ import annotations

from . import data
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
    SimulationMode,
    TradeRecord,
    ValidationDecision,
    ValidationDecisionReport,
)
from .optimization import ConfigApplicator, RecommendationEngine
from .validation import attach_validation_decision, evaluate_promotion_validation

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestFilterConfig",
    "build_backtest_components",
    "data",
    "CachedDataLoader",
    "BacktestFilterSimulator",
    "BacktestFilterStats",
    "BacktestMetrics",
    "BacktestResult",
    "ConfigApplicator",
    "SimulationMode",
    "ParamChange",
    "PortfolioTracker",
    "ParameterSpace",
    "Recommendation",
    "RecommendationEngine",
    "RecommendationStatus",
    "SignalEvaluation",
    "TradeRecord",
    "ValidationDecision",
    "ValidationDecisionReport",
    "attach_validation_decision",
    "evaluate_promotion_validation",
]
