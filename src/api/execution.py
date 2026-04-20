"""Execution domain composite root (P9 Phase 1).

Aggregates execution sub-routers (currently: workbench).
"""

from __future__ import annotations

from fastapi import APIRouter

from .execution_routes import workbench_router
from .execution_routes.workbench import execution_workbench

router = APIRouter(tags=["execution"])
router.include_router(workbench_router)

__all__ = ["execution_workbench", "router"]
