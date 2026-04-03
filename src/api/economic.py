from __future__ import annotations

from fastapi import APIRouter

from .economic_routes import calendar_router, impact_router
from .economic_routes.calendar import (
    calendar_history,
    calendar_risk_windows,
    calendar_status,
    calendar_trade_guard,
    calendar_updates,
    curated_calendar,
    high_impact_calendar,
    merged_calendar_risk_windows,
    refresh_calendar,
    upcoming_calendar,
)
from .economic_routes.impact import (
    calendar_enriched,
    market_impact_detail,
    market_impact_stats,
    market_impact_status,
    market_impact_upcoming,
)

router = APIRouter(tags=["economic"])
router.include_router(calendar_router)
router.include_router(impact_router)

__all__ = [
    "calendar_enriched",
    "calendar_history",
    "calendar_risk_windows",
    "calendar_status",
    "calendar_trade_guard",
    "calendar_updates",
    "curated_calendar",
    "high_impact_calendar",
    "market_impact_detail",
    "market_impact_stats",
    "market_impact_status",
    "market_impact_upcoming",
    "merged_calendar_risk_windows",
    "refresh_calendar",
    "router",
    "upcoming_calendar",
]
