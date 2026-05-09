"""Shared reason constants used across trading submodules."""

from __future__ import annotations

# Trade guards / controls.
REASON_XAUUSD_TRADE_GUARD_BLOCKED = "xauusd_trade_guard_blocked"
REASON_CLOSE_ONLY_MODE_ENABLED = "close_only_mode_enabled"
REASON_AUTO_ENTRY_PAUSED = "auto_entry_paused"

# Pending entry lifecycle.
REASON_TRADED_THIS_BAR = "traded_this_bar"
REASON_SHUTDOWN = "shutdown"
REASON_FILL_QUEUE_OVERFLOW = "fill_queue_overflow"
REASON_NEW_SIGNAL_OVERRIDE = "new_signal_override"
REASON_PENDING_TIMEOUT = "timeout"
# 通用超时原因（用于持仓超时退出等场景，与 REASON_PENDING_TIMEOUT 同值）。
REASON_TIMEOUT = "timeout"
REASON_MISSING_WITHOUT_FILL = "missing_without_fill"
REASON_STARTUP_EXPIRED = "startup_expired"
REASON_STARTUP_MISSING = "startup_missing"
REASON_STARTUP_MT5_MISSING = "startup_mt5_missing"
REASON_STARTUP_ORPHAN_CANCELLED = "startup_orphan_cancelled"
# ADR-013 P4-residual: recovery 检测到同 group 中 sibling 已 fill 但本 row 仍 placed
# → 上次 fill 后 cancel 未持久化，本次重启立即撤。
REASON_STARTUP_OCO_SIBLING_FILLED = "startup_oco_sibling_filled"

# Recovery / lifecycle reason.
REASON_END_OF_DAY = "end_of_day"

# Position close reason.
REASON_STOP_LOSS = "stop_loss"
REASON_TAKE_PROFIT = "take_profit"
REASON_TRAILING_STOP = "trailing_stop"
REASON_SIGNAL_EXIT = "signal_exit"
# Broker tick 触发关闭但 close_price 不在 SL/TP 阈值内（可能 manual close / margin call /
# 系统重启时 reconcile 检测到的旧 close）。区别于 SL/TP 推断成功的明确出场。
REASON_BROKER_CLOSE = "broker_close"

# Position/state status reason.
REASON_PLACED_BY_EXECUTOR = "placed_by_executor"
REASON_MATCHED_LIVE_POSITION = "matched_live_position"
REASON_MATCHED_TRACKED_POSITION = "matched_tracked_position"
REASON_RECOVERED_FROM_MT5_WITHOUT_LOCAL_STATE = "recovered_from_mt5_without_local_state"


__all__ = [
    "REASON_XAUUSD_TRADE_GUARD_BLOCKED",
    "REASON_CLOSE_ONLY_MODE_ENABLED",
    "REASON_AUTO_ENTRY_PAUSED",
    "REASON_TRADED_THIS_BAR",
    "REASON_SHUTDOWN",
    "REASON_FILL_QUEUE_OVERFLOW",
    "REASON_NEW_SIGNAL_OVERRIDE",
    "REASON_PENDING_TIMEOUT",
    "REASON_TIMEOUT",
    "REASON_MISSING_WITHOUT_FILL",
    "REASON_STARTUP_EXPIRED",
    "REASON_STARTUP_MISSING",
    "REASON_STARTUP_MT5_MISSING",
    "REASON_STARTUP_ORPHAN_CANCELLED",
    "REASON_STARTUP_OCO_SIBLING_FILLED",
    "REASON_END_OF_DAY",
    "REASON_STOP_LOSS",
    "REASON_TAKE_PROFIT",
    "REASON_TRAILING_STOP",
    "REASON_BROKER_CLOSE",
    "REASON_SIGNAL_EXIT",
    "REASON_PLACED_BY_EXECUTOR",
    "REASON_MATCHED_LIVE_POSITION",
    "REASON_MATCHED_TRACKED_POSITION",
    "REASON_RECOVERED_FROM_MT5_WITHOUT_LOCAL_STATE",
]
