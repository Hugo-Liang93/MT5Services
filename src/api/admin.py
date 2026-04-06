from __future__ import annotations

from fastapi import APIRouter

from .admin_routes import config_router, dashboard_router, strategies_router, streams_router
from .admin_routes.common import build_strategy_detail as _build_strategy_detail
from .admin_routes.common import load_json_config as _load_json_config
from .admin_routes.config import (
    admin_config,
    admin_config_indicators,
    admin_config_risk,
    admin_config_signal,
)
from .admin_routes.dashboard import admin_dashboard
from .admin_routes.strategies import (
    admin_confidence_pipeline,
    admin_performance_strategies,
    admin_strategies,
    admin_strategy_detail,
)
from .admin_routes.streams import admin_events_stream, admin_pipeline_stats, admin_pipeline_stream

router = APIRouter(tags=["admin"])
router.include_router(dashboard_router)
router.include_router(config_router)
router.include_router(strategies_router)
router.include_router(streams_router)

__all__ = [
    "_build_strategy_detail",
    "_load_json_config",
    "admin_config",
    "admin_config_indicators",
    "admin_config_risk",
    "admin_config_signal",
    "admin_confidence_pipeline",
    "admin_dashboard",
    "admin_events_stream",
    "admin_performance_strategies",
    "admin_pipeline_stats",
    "admin_pipeline_stream",
    "admin_strategies",
    "admin_strategy_detail",
    "router",
]
