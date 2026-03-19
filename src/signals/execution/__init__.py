"""信号执行子模块

包含信号过滤链、仓位大小计算和信号状态机。
"""

from .filters import (
    EconomicEventFilter,
    SessionFilter,
    SignalFilterChain,
    SpreadFilter,
    TradeGuardProvider,
)
from .policy import RuntimeSignalState, SignalPolicy
from .sizing import TradeParameters, compute_trade_params, extract_atr_from_indicators

__all__ = [
    "EconomicEventFilter",
    "RuntimeSignalState",
    "SessionFilter",
    "SignalFilterChain",
    "SignalPolicy",
    "SpreadFilter",
    "TradeGuardProvider",
    "TradeParameters",
    "compute_trade_params",
    "extract_atr_from_indicators",
]
