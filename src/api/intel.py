"""Intel 域组合根（P10.2）。"""

from __future__ import annotations

from fastapi import APIRouter

from .intel_routes import intel_action_queue_router
from .intel_routes.action_queue import intel_action_queue

router = APIRouter(tags=["intel"])
router.include_router(intel_action_queue_router)

__all__ = [
    "intel_action_queue",
    "router",
]
