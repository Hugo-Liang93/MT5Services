from __future__ import annotations

from fastapi import APIRouter

from .trade_routes import (
    commands_router,
    runtime_router,
    state_router,
    trace_router,
    trades_workbench_router,
)
from .trade_routes.commands import (
    cancel_orders,
    cancel_orders_batch,
    close,
    close_all,
    close_batch,
    estimate_margin,
    modify_orders,
    modify_positions,
    trade,
    trade_batch,
    trade_closeout_exposure,
    trade_control_update,
    trade_dispatch,
    trade_from_signal,
    trade_precheck,
    trade_reconcile,
)
from .trade_routes.runtime import trade_runtime_mode_status, trade_runtime_mode_update
from .trade_routes.state import (
    orders,
    positions,
    get_sl_tp_history,
    trade_active_pending_state_list,
    trade_command_audit_detail,
    trade_command_audits,
    trade_control_status,
    trade_daily_summary,
    trade_entry_status,
    trade_pending_execution_context_list,
    trade_pending_lifecycle_state_list,
    trade_position_state_list,
    trade_state_alerts_summary,
    trade_state_closeout_summary,
    trade_state_stream,
    trade_state_summary,
    trading_accounts,
)
from .trade_routes.trace import trade_trace_by_signal_id, trade_trace_by_trace_id, trade_traces
from .trade_routes.trades_workbench import trade_detail, trades_workbench

router = APIRouter(tags=["trade"])
router.include_router(commands_router)
router.include_router(runtime_router)
router.include_router(state_router)
router.include_router(trace_router)
router.include_router(trades_workbench_router)

__all__ = [
    "cancel_orders",
    "cancel_orders_batch",
    "close",
    "close_all",
    "close_batch",
    "estimate_margin",
    "get_sl_tp_history",
    "modify_orders",
    "modify_positions",
    "orders",
    "positions",
    "router",
    "trade",
    "trade_active_pending_state_list",
    "trade_batch",
    "trade_closeout_exposure",
    "trade_command_audit_detail",
    "trade_command_audits",
    "trade_control_status",
    "trade_control_update",
    "trade_daily_summary",
    "trade_dispatch",
    "trade_entry_status",
    "trade_from_signal",
    "trade_pending_execution_context_list",
    "trade_pending_lifecycle_state_list",
    "trade_position_state_list",
    "trade_precheck",
    "trade_reconcile",
    "trade_runtime_mode_status",
    "trade_runtime_mode_update",
    "trade_state_alerts_summary",
    "trade_state_closeout_summary",
    "trade_state_stream",
    "trade_state_summary",
    "trade_detail",
    "trade_trace_by_signal_id",
    "trade_trace_by_trace_id",
    "trade_traces",
    "trades_workbench",
    "trades_workbench_router",
    "trading_accounts",
]
