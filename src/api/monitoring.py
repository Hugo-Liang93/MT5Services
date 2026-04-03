from __future__ import annotations

from fastapi import APIRouter

from .monitoring_routes import health_router, runtime_router
from .monitoring_routes.health import (
    TRADE_TRIGGER_METHODS,
    get_health_status,
    get_monitored_components,
    get_performance_stats,
    get_queue_stats,
    get_system_status,
    get_trading_trigger_methods,
    get_event_stats,
    get_metrics,
    health_live,
    health_ready,
    resolve_alert,
    reset_failed_events,
    cleanup_old_events,
    trigger_consistency_check,
)
from .monitoring_routes.runtime import (
    cancel_pending_entries_by_symbol,
    cancel_pending_entry,
    get_effective_runtime_config,
    get_economic_calendar_monitoring,
    get_pending_entries,
    get_runtime_tasks,
    get_startup_monitoring,
    get_trading_monitoring,
    trigger_config_reload,
)

router = APIRouter(tags=["monitoring"])
router.include_router(health_router)
router.include_router(runtime_router)

__all__ = [
    "TRADE_TRIGGER_METHODS",
    "cancel_pending_entries_by_symbol",
    "cancel_pending_entry",
    "cleanup_old_events",
    "get_effective_runtime_config",
    "get_economic_calendar_monitoring",
    "get_event_stats",
    "get_health_status",
    "get_metrics",
    "get_monitored_components",
    "get_pending_entries",
    "get_performance_stats",
    "get_queue_stats",
    "get_runtime_tasks",
    "get_startup_monitoring",
    "get_system_status",
    "get_trading_monitoring",
    "get_trading_trigger_methods",
    "health_live",
    "health_ready",
    "resolve_alert",
    "reset_failed_events",
    "router",
    "trigger_config_reload",
    "trigger_consistency_check",
]
