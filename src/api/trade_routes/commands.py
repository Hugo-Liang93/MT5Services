from __future__ import annotations

from fastapi import APIRouter

from .command_routes import direct_router, operator_router, signal_router
from .command_routes.direct import (
    estimate_margin,
    modify_orders,
    modify_positions,
    trade,
    trade_batch,
    trade_dispatch,
    trade_precheck,
    trade_reconcile,
)
from .command_routes.operator import (
    cancel_orders,
    cancel_orders_batch,
    close,
    close_all,
    close_batch,
    trade_closeout_exposure,
    trade_control_update,
)
from .command_routes.signal import trade_from_signal

router = APIRouter(tags=["trade"])
router.include_router(direct_router)
router.include_router(signal_router)
router.include_router(operator_router)

__all__ = [
    "cancel_orders",
    "cancel_orders_batch",
    "close",
    "close_all",
    "close_batch",
    "estimate_margin",
    "modify_orders",
    "modify_positions",
    "router",
    "trade",
    "trade_batch",
    "trade_closeout_exposure",
    "trade_control_update",
    "trade_dispatch",
    "trade_from_signal",
    "trade_precheck",
    "trade_reconcile",
]
