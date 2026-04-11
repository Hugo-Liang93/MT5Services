"""统一的交易状态事件与原因常量。

目标：执行链路与持仓链路共享同一套状态语义，
避免跨模块散落字符串带来的观测口径漂移。
"""

from __future__ import annotations

# PositionManager 回调语义（on_position_*）
POSITION_TRACKED = "tracked"
POSITION_UPDATED = "position_updated"
POSITION_RECOVERED = "recovered"
POSITION_PARTIALLY_CLOSED = "partial_close"

# 平仓来源语义（close_source / close 复盘字段）
POSITION_CLOSE_SOURCE_MT5_MISSING = "mt5_missing"
POSITION_CLOSE_SOURCE_HISTORY_DEALS = "history_deals"

# SL / TP 追踪与更新语义
POSITION_UPDATE_REASON_CHANDELIER_TRAIL = "chandelier_trail"
POSITION_UPDATE_REASON_TRAILING_SL = "trailing_sl"
POSITION_UPDATE_REASON_TRAILING_TP = "trailing_tp"


__all__ = [
    "POSITION_TRACKED",
    "POSITION_UPDATED",
    "POSITION_RECOVERED",
    "POSITION_PARTIALLY_CLOSED",
    "POSITION_CLOSE_SOURCE_MT5_MISSING",
    "POSITION_CLOSE_SOURCE_HISTORY_DEALS",
    "POSITION_UPDATE_REASON_CHANDELIER_TRAIL",
    "POSITION_UPDATE_REASON_TRAILING_SL",
    "POSITION_UPDATE_REASON_TRAILING_TP",
]
