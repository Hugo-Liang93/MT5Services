from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Dict, TypeVar

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
_T = TypeVar("_T")


def _execute_monitored_call(
    label: str,
    operation: Callable[[], _T],
    *,
    fallback: _T,
    allow_fallback: bool = False,
) -> _T:
    try:
        return operation()
    except FileNotFoundError as exc:
        logger.warning("%s unavailable: %s", label, exc)
        if allow_fallback:
            return fallback
        raise HTTPException(status_code=404, detail=f"{label} not found: {exc}") from exc
    except (AssertionError, AttributeError, KeyError, RuntimeError, TypeError, ValueError) as exc:
        logger.warning("%s failed with expected error: %s", label, exc)
        if allow_fallback:
            return fallback
        raise HTTPException(status_code=500, detail=f"{label} failed: {exc}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("%s failed with unexpected error", label, exc_info=True)
        if allow_fallback:
            return fallback
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _enum_or_raw(value: Any) -> str:
    return getattr(value, "value", value)


@router.get("/config/effective", response_model=ApiResponse[EffectiveRuntimeConfigView], summary="获取当前有效运行配置")
async def get_effective_runtime_config() -> ApiResponse[EffectiveRuntimeConfigView]:
    indicator_manager = _execute_monitored_call(
        "indicator manager",
        get_indicator_manager,
        fallback=None,
    )
    snapshot = _execute_monitored_call(
        "effective config snapshot",
        get_effective_config_snapshot,
        fallback={},
    )
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


@router.get("/economic-calendar", summary="获取经济日历监控摘要")
async def get_economic_calendar_monitoring() -> ApiResponse[Dict[str, Any]]:
    health_monitor = _execute_monitored_call(
        "health monitor",
        get_health_monitor_instance,
        fallback=None,
    )
    service = _execute_monitored_call(
        "economic calendar service",
        get_economic_calendar_service,
        fallback=None,
    )
    return ApiResponse.success_response(
        {
            "service": service.stats(),
            "metrics": {
                "staleness": health_monitor.get_recent_metrics("economic_calendar", "economic_calendar_staleness", 50),
                "provider_failures": health_monitor.get_recent_metrics(
                    "economic_calendar",
                    "economic_provider_failures",
                    50,
                ),
            },
        }
    )


@router.get("/trading", summary="获取交易监控摘要")
async def get_trading_monitoring(hours: int = 24) -> ApiResponse[Dict[str, Any]]:
    return ApiResponse.success_response(
        _execute_monitored_call(
            "runtime trading summary",
            lambda: get_runtime_read_model().trading_summary(hours=hours),
            fallback={},
        )
    )


@router.get("/startup", summary="获取启动阶段与运行状态摘要")
async def get_startup_monitoring() -> ApiResponse[Dict[str, Any]]:
    status = get_startup_status()
    ingestor = _execute_monitored_call("ingestor", get_ingestor, fallback=None)
    indicator_manager = _execute_monitored_call("indicator manager", get_indicator_manager, fallback=None)
    economic_calendar_service = _execute_monitored_call(
        "economic calendar service",
        get_economic_calendar_service,
        fallback=None,
    )
    monitoring_manager = _execute_monitored_call("monitoring manager", get_monitoring_manager_instance, fallback=None)

    queue_stats = _execute_monitored_call(
        "ingestor queue stats",
        ingestor.queue_stats,
        fallback={"threads": {}},
        allow_fallback=True,
    )
    if not isinstance(queue_stats, dict):
        queue_stats = {}

    performance_stats = _execute_monitored_call(
        "indicator performance stats",
        indicator_manager.get_performance_stats,
        fallback={},
        allow_fallback=True,
    )
    if not isinstance(performance_stats, dict):
        performance_stats = {}

    status["runtime"] = {
        "monitoring_registered": True,
        "components": {
            "data_ingestion": bool(queue_stats.get("threads", {}).get("ingest_alive", False)),
            "indicator_calculation": bool(performance_stats.get("event_loop_running", False)),
            "economic_calendar": economic_calendar_service.stats().get("running") if economic_calendar_service else False,
            "monitoring": bool(monitoring_manager),
        },
    }
    return ApiResponse.success_response(status)


@router.post("/config/reload", summary="手动触发配置热加载")
async def trigger_config_reload(filename: str = "signal.ini") -> ApiResponse[ConfigReloadView]:
    _execute_monitored_call("config reload", reload_configs, fallback=None)
    manager = _execute_monitored_call("file config manager", get_file_config_manager, fallback=None)
    reloaded = _execute_monitored_call(
        f"file config reload {filename}",
        lambda: manager.reload(filename),
        fallback=False,
    )
    if not reloaded:
        raise HTTPException(status_code=404, detail=f"config file not found: {filename}")
    return ApiResponse.success_response(
        ConfigReloadView(success=True, reloaded=filename, cache_cleared=True).model_dump()
    )


@router.get("/runtime-tasks", summary="获取运行时任务状态")
async def get_runtime_tasks(component: str | None = None, task_name: str | None = None) -> ApiResponse[RuntimeTasksView]:
    return ApiResponse.success_response(
        {
            "items": _execute_monitored_call(
                "runtime task status",
                lambda: get_runtime_task_status(component=component, task_name=task_name),
                fallback=[],
                allow_fallback=True,
            ),
            "filters": {"component": component, "task_name": task_name},
        }
    )


@router.get("/pending-entries", summary="查询当前挂起入场")
async def get_pending_entries() -> ApiResponse[Dict[str, Any]]:
    return ApiResponse.success_response(
        _execute_monitored_call(
            "runtime pending entries summary",
            lambda: get_runtime_read_model().pending_entries_summary(),
            fallback={},
            allow_fallback=True,
        )
    )


@router.post("/pending-entries/{signal_id}/cancel", response_model=ApiResponse[PendingEntryCancellationView], summary="取消指定挂起入场")
async def cancel_pending_entry(signal_id: str, reason: str = "api") -> ApiResponse[PendingEntryCancellationView]:
    cancelled = _execute_monitored_call(
        f"cancel pending entry {signal_id}",
        lambda: get_pending_entry_manager().cancel(signal_id, reason=reason),
        fallback=False,
    )
    return ApiResponse.success_response(
        PendingEntryCancellationView(cancelled=cancelled, signal_id=signal_id, reason=reason)
    )


@router.post("/pending-entries/cancel-by-symbol", response_model=ApiResponse[PendingEntriesBySymbolCancellationView], summary="按品种取消全部挂起入场")
async def cancel_pending_entries_by_symbol(symbol: str, reason: str = "api") -> ApiResponse[PendingEntriesBySymbolCancellationView]:
    count = _execute_monitored_call(
        f"cancel pending entries for {symbol}",
        lambda: get_pending_entry_manager().cancel_by_symbol(symbol, reason=reason),
        fallback=0,
    )
    return ApiResponse.success_response(
        PendingEntriesBySymbolCancellationView(cancelled_count=count, symbol=symbol, reason=reason)
    )
