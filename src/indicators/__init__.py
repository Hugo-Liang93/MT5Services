"""
统一指标模块

整合了基础指标函数和高级功能：
1. 基础指标计算（均值、动量、波动率、成交量）
2. 增量计算引擎
3. 智能缓存系统
4. 性能监控
5. 依赖关系管理
6. 并行计算框架
7. 统一配置管理
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core.mean import sma, ema, wma
    from .core.momentum import cci, macd, roc, rsi
    from .core.volatility import atr, bollinger, donchian, keltner
    from .core.volume import obv, vwap
    from .cache.smart_cache import SmartCache, get_global_cache, get_global_cache_stats
    from .monitoring.metrics_collector import (
        MetricsCollector, IndicatorMetrics, AggregatedMetrics,
        get_global_collector, record_indicator_computation, get_performance_report
    )
    from .engine.dependency_manager import DependencyManager, get_global_dependency_manager
    from .engine.parallel_executor import ParallelExecutor, TaskResult, TaskStatus, get_global_executor
    from .engine.pipeline_v2 import OptimizedPipeline, PipelineConfig, get_global_pipeline
    from .config.config import (
        UnifiedIndicatorConfig, IndicatorConfig, PipelineConfig as PipelineConfigType,
        ComputeMode, CacheStrategy, get_global_config_manager, get_config
    )
    from .manager import UnifiedIndicatorManager, get_global_unified_manager

__all__ = [
    # 基础指标函数
    "sma", "ema", "wma",
    "rsi", "macd", "roc", "cci",
    "atr", "bollinger", "keltner", "donchian",
    "obv", "vwap",
    
    # 缓存系统
    "SmartCache",
    "get_global_cache",
    "get_global_cache_stats",
    
    # 性能监控
    "MetricsCollector",
    "IndicatorMetrics",
    "AggregatedMetrics",
    "get_global_collector",
    "record_indicator_computation",
    "get_performance_report",
    
    # 依赖关系管理
    "DependencyManager",
    "get_global_dependency_manager",
    
    # 并行计算
    "ParallelExecutor",
    "TaskResult",
    "TaskStatus",
    "get_global_executor",
    
    # 优化流水线
    "OptimizedPipeline",
    "PipelineConfig",
    "get_global_pipeline",
    
    # 统一配置系统
    "UnifiedIndicatorConfig",
    "IndicatorConfig",
    "PipelineConfigType",
    "ComputeMode",
    "CacheStrategy",
    "get_global_config_manager",
    "get_config",
    
    # 统一指标管理器
    "UnifiedIndicatorManager",
    "get_global_unified_manager",
]


def __getattr__(name: str):
    """延迟导入基础指标函数，避免循环导入"""
    # 基础指标函数
    if name in {"sma", "ema", "wma"}:
        from .core import mean
        return {"sma": mean.sma, "ema": mean.ema, "wma": mean.wma}[name]
    if name in {"rsi", "macd", "roc", "cci"}:
        from .core import momentum
        return {"rsi": momentum.rsi, "macd": momentum.macd, "roc": momentum.roc, "cci": momentum.cci}[name]
    if name in {"atr", "bollinger", "keltner", "donchian"}:
        from .core import volatility
        return {
            "atr": volatility.atr,
            "bollinger": volatility.bollinger,
            "keltner": volatility.keltner,
            "donchian": volatility.donchian,
        }[name]
    if name in {"obv", "vwap"}:
        from .core import volume
        return {"obv": volume.obv, "vwap": volume.vwap}[name]
    
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# 版本信息
__version__ = "3.0.0"
__description__ = "统一指标计算模块，整合基础函数与高级功能"