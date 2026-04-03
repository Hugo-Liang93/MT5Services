from __future__ import annotations

from fastapi import APIRouter

from .account_routes import queries_router
from .account_routes.queries import (
    account_info,
    account_list,
    account_orders,
    account_positions,
)

router = APIRouter(tags=["account"])
router.include_router(queries_router)

__all__ = [
    "account_info",
    "account_list",
    "account_orders",
    "account_positions",
    "router",
]
