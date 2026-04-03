"""Studio API 组合根。"""

from __future__ import annotations

from fastapi import APIRouter

from .studio_routes import rest_router, stream_router
from .studio_routes.rest import (
    studio_agent_detail,
    studio_agents,
    studio_events,
    studio_summary,
)
from .studio_routes.stream import studio_stream

router = APIRouter(tags=["studio"])
router.include_router(rest_router)
router.include_router(stream_router)

__all__ = [
    "router",
    "studio_agent_detail",
    "studio_agents",
    "studio_events",
    "studio_stream",
    "studio_summary",
]
