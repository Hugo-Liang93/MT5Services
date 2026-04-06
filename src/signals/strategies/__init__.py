"""信号策略子模块

包含所有策略实现、适配器和高时间框架缓存。

策略按类型分布在：
  base.py              — SignalStrategy 协议 + 工具函数
  structured/          — 结构化策略（Why/When/Where 三层评估）
"""

from .adapters import IndicatorSource, UnifiedIndicatorSourceAdapter
from .base import SignalStrategy, StrategyCategory, _resolve_indicator_value
from .htf_cache import HTFStateCache
from .structured import (
    StructuredBreakoutFollow,
    StructuredLowbarEntry,
    StructuredRangeReversion,
    StructuredSessionBreakout,
    StructuredSweepReversal,
    StructuredTrendContinuation,
    StructuredTrendlineTouch,
)

__all__ = [
    "HTFStateCache",
    "IndicatorSource",
    "SignalStrategy",
    "StrategyCategory",
    "StructuredBreakoutFollow",
    "StructuredLowbarEntry",
    "StructuredRangeReversion",
    "StructuredSessionBreakout",
    "StructuredSweepReversal",
    "StructuredTrendContinuation",
    "StructuredTrendlineTouch",
    "UnifiedIndicatorSourceAdapter",
]
