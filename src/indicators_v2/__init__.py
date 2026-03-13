"""
优化后的指标模块 v2

包含：
1. 增量计算引擎
2. 智能缓存系统
3. 性能监控
4. 依赖关系管理
5. 并行计算框架
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base.incremental import IncrementalIndicator, IndicatorState, SimpleIndicator
    from .base.smart_cache import SmartCache, get_global_cache, get_global_cache_stats
    from .monitoring.metrics_collector import (
        MetricsCollector, IndicatorMetrics, AggregatedMetrics,
        get_global_collector, record_indicator_computation, get_performance_report
    )
    from .engine.dependency_manager import DependencyManager, get_global_dependency_manager
    from .engine.parallel_executor import ParallelExecutor, TaskResult, TaskStatus, get_global_executor
    from .engine.pipeline_v2 import OptimizedPipeline, PipelineConfig, get_global_pipeline

__all__ = [
    # 基础类
    "IncrementalIndicator",
    "IndicatorState",
    "SimpleIndicator",
    
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
    
    # 向后兼容
    "get_legacy_indicator",
]


def get_legacy_indicator(name: str, params: dict):
    """
    获取传统指标函数（向后兼容）
    
    Args:
        name: 指标名称
        params: 指标参数
        
    Returns:
        传统指标函数
    """
    # 这里实现向后兼容逻辑
    # 暂时返回None，后续实现
    return None


# 版本信息
__version__ = "2.0.0"
__description__ = "优化后的指标计算模块，支持增量计算、智能缓存和性能监控"