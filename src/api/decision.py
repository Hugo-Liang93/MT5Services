from __future__ import annotations

from fastapi import APIRouter

from .decision_routes import brief_router
from .decision_routes.brief import decision_brief

router = APIRouter(tags=["decision"])
router.include_router(brief_router)

__all__ = [
    "decision_brief",
    "router",
]
