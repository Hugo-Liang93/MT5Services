"""Indicator computation engine exports."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .dependency_manager import DependencyManager, get_global_dependency_manager
    from .parallel_executor import ParallelExecutor, TaskResult, TaskStatus, get_global_executor
    from .pipeline import OptimizedPipeline, PipelineConfig, get_global_pipeline

__all__ = [
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
]


__version__ = "1.0.0"
__description__ = "Indicator computation engine exports."
