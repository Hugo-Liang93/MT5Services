from __future__ import annotations

from fastapi import APIRouter

from .state_routes import audit_router, list_router, overview_router, stream_router
from .state_routes.audit import get_sl_tp_history, trade_command_audits
from .state_routes.lists import (
    trade_active_pending_state_list,
    trade_pending_execution_context_list,
    trade_pending_lifecycle_state_list,
    trade_position_state_list,
)
from .state_routes.overview import (
    orders,
    positions,
    trade_control_status,
    trade_daily_summary,
    trade_entry_status,
    trade_state_alerts_summary,
    trade_state_closeout_summary,
    trade_state_summary,
    trading_accounts,
)
from .state_routes.stream import trade_state_stream

router = APIRouter(tags=["trade"])
router.include_router(overview_router)
router.include_router(list_router)
router.include_router(audit_router)
router.include_router(stream_router)

__all__ = [
    "get_sl_tp_history",
    "orders",
    "positions",
    "router",
    "trade_active_pending_state_list",
    "trade_command_audits",
    "trade_control_status",
    "trade_daily_summary",
    "trade_entry_status",
    "trade_pending_execution_context_list",
    "trade_pending_lifecycle_state_list",
    "trade_position_state_list",
    "trade_state_alerts_summary",
    "trade_state_closeout_summary",
    "trade_state_stream",
    "trade_state_summary",
    "trading_accounts",
]
