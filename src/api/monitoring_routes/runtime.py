from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from src.api.deps import (
    get_economic_calendar_service,
    get_health_monitor_instance,
    get_indicator_manager,
    get_ingestor,
    get_monitoring_manager_instance,
    get_pending_entry_manager,
    get_runtime_read_model,
    get_runtime_task_status,
    get_startup_status,
)
from src.api.schemas import ApiResponse
from src.config import get_effective_config_snapshot, reload_configs
from src.config.file_manager import get_file_config_manager

from .health import TRADE_TRIGGER_METHODS
from .view_models import (
    ConfigReloadView,
    EffectiveRuntimeConfigView,
    PendingEntriesBySymbolCancellationView,
    PendingEntryCancellationView,
    RuntimeTasksView,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/monitoring", tags=["monitoring"])


def _enum_or_raw(value: Any) -> str:
    return getattr(value, "value", value)


@router.get("/config/effective", response_model=ApiResponse[EffectiveRuntimeConfigView], summary="获取当前有效运行配置")
async def get_effective_runtime_config() -> ApiResponse[EffectiveRuntimeConfigView]:
    try:
        indicator_manager = get_indicator_manager()
        snapshot = get_effective_config_snapshot()
        snapshot["indicator_scope"] = {
            "symbols": list(indicator_manager.config.symbols),
            "timeframes": list(indicator_manager.config.timeframes),
            "inherit_symbols": indicator_manager.config.inherit_symbols,
            "inherit_timeframes": indicator_manager.config.inherit_timeframes,
            "indicator_reload_interval": indicator_manager.config.reload_interval,
            "indicator_poll_interval": indicator_manager.config.pipeline.poll_interval,
            "indicator_cache_maxsize": indicator_manager.config.pipeline.cache_maxsize,
            "indicator_cache_strategy": _enum_or_raw(indicator_manager.config.pipeline.cache_strategy),
        }
        return ApiResponse.success_response(EffectiveRuntimeConfigView(**snapshot))
    except Exception as exc:
        logger.error("Failed to get effective runtime config: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/economic-calendar", summary="获取经济日历监控摘要")
async def get_economic_calendar_monitoring() -> Dict[str, Any]:
    try:
        health_monitor = get_health_monitor_instance()
        service = get_economic_calendar_service()
        return {
            "service": service.stats(),
            "metrics": {
                "staleness": health_monitor.get_recent_metrics("economic_calendar", "economic_calendar_staleness", 50),
                "provider_failures": health_monitor.get_recent_metrics("economic_calendar", "economic_provider_failures", 50),
            },
        }
    except Exception as exc:
        logger.error("Failed to get economic calendar monitoring summary: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/trading", summary="获取交易监控摘要")
async def get_trading_monitoring(hours: int = 24) -> Dict[str, Any]:
    try:
        return get_runtime_read_model().trading_summary(hours=hours)
    except Exception as exc:
        logger.error("Failed to get trading monitoring summary: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/startup", summary="获取启动阶段与运行状态摘要")
async def get_startup_monitoring() -> Dict[str, Any]:
    try:
        status = get_startup_status()
        status["runtime"] = {
            "monitoring_registered": True,
            "components": {
                "data_ingestion": get_ingestor().queue_stats()["threads"].get("ingest_alive", False),
                "indicator_calculation": get_indicator_manager().get_performance_stats().get("event_loop_running", False),
                "economic_calendar": get_economic_calendar_service().stats().get("running"),
                "monitoring": bool(get_monitoring_manager_instance()),
            },
        }
        return status
    except Exception as exc:
        logger.error("Failed to get startup monitoring summary: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/config/reload", response_model=ConfigReloadView, summary="手动触发配置热加载")
async def trigger_config_reload(filename: str = "signal.ini") -> Dict[str, Any]:
    try:
        reload_configs()
        manager = get_file_config_manager()
        reloaded = manager.reload(filename)
        if not reloaded:
            raise FileNotFoundError(filename)
        return ConfigReloadView(success=True, reloaded=filename, cache_cleared=True).model_dump()
    except Exception as exc:
        logger.error("Config reload failed for %s: %s", filename, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/runtime-tasks", response_model=RuntimeTasksView, summary="获取运行时任务状态")
async def get_runtime_tasks(component: str | None = None, task_name: str | None = None) -> Dict[str, Any]:
    try:
        return {
            "items": get_runtime_task_status(component=component, task_name=task_name),
            "filters": {"component": component, "task_name": task_name},
        }
    except Exception as exc:
        logger.error("Failed to get runtime task status: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/pending-entries", summary="查询当前挂起入场")
async def get_pending_entries() -> ApiResponse[Dict[str, Any]]:
    try:
        return ApiResponse.success_response(get_runtime_read_model().pending_entries_summary())
    except Exception as exc:
        logger.error("Failed to get pending entries: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/pending-entries/{signal_id}/cancel", response_model=ApiResponse[PendingEntryCancellationView], summary="取消指定挂起入场")
async def cancel_pending_entry(signal_id: str, reason: str = "api") -> ApiResponse[PendingEntryCancellationView]:
    try:
        cancelled = get_pending_entry_manager().cancel(signal_id, reason=reason)
        return ApiResponse.success_response(PendingEntryCancellationView(cancelled=cancelled, signal_id=signal_id, reason=reason))
    except Exception as exc:
        logger.error("Failed to cancel pending entry %s: %s", signal_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/pending-entries/cancel-by-symbol", response_model=ApiResponse[PendingEntriesBySymbolCancellationView], summary="按品种取消全部挂起入场")
async def cancel_pending_entries_by_symbol(symbol: str, reason: str = "api") -> ApiResponse[PendingEntriesBySymbolCancellationView]:
    try:
        count = get_pending_entry_manager().cancel_by_symbol(symbol, reason=reason)
        return ApiResponse.success_response(
            PendingEntriesBySymbolCancellationView(cancelled_count=count, symbol=symbol, reason=reason)
        )
    except Exception as exc:
        logger.error("Failed to cancel pending entries for %s: %s", symbol, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
