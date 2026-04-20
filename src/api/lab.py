"""Lab 域组合根（P10.5）。"""

from __future__ import annotations

from fastapi import APIRouter

from .lab_routes import lab_impact_router
from .lab_routes.impact import lab_impact

router = APIRouter(tags=["lab"])
router.include_router(lab_impact_router)

__all__ = ["lab_impact", "router"]
