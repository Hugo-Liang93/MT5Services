from __future__ import annotations

from fastapi import APIRouter

from .indicators_routes import catalog_router, values_router
from .indicators_routes.catalog import (
    clear_cache,
    get_dependency_graph,
    get_performance_stats,
    list_available_indicators,
)
from .indicators_routes.models import IndicatorRequest, IndicatorResponse, IndicatorValue
from .indicators_routes.values import (
    compute_indicators,
    get_indicator,
    get_indicators,
    get_intrabar_indicators,
)

router = APIRouter(tags=["indicators"])
router.include_router(catalog_router)
router.include_router(values_router)

__all__ = [
    "IndicatorRequest",
    "IndicatorResponse",
    "IndicatorValue",
    "clear_cache",
    "compute_indicators",
    "get_dependency_graph",
    "get_indicator",
    "get_indicators",
    "get_intrabar_indicators",
    "get_performance_stats",
    "list_available_indicators",
    "router",
]
