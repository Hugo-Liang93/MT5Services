"""
指标计算引擎模块

包含：
1. 依赖关系管理
2. 并行计算框架
3. 优化计算流水线
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .dependency_manager import DependencyManager, get_global_dependency_manager
    from .parallel_executor import ParallelExecutor, TaskResult, TaskStatus, get_global_executor
    from .pipeline_v2 import OptimizedPipeline, PipelineConfig, get_global_pipeline

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


# 版本信息
__version__ = "2.0.0"
__description__ = "指标计算引擎模块，支持依赖管理、并行计算和优化流水线"