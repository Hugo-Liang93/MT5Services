"""Cockpit 域组合根（P10.1）。"""

from __future__ import annotations

from fastapi import APIRouter

from .cockpit_routes import cockpit_overview_router
from .cockpit_routes.overview import cockpit_overview

router = APIRouter(tags=["cockpit"])
router.include_router(cockpit_overview_router)

__all__ = [
    "cockpit_overview",
    "router",
]
