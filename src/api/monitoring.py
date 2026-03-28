"""Monitoring API routes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

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

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/monitoring", tags=["monitoring"])

TRADE_TRIGGER_METHODS = [
    {
        "id": "trade_api_direct",
        "type": "api",
        "path": "/v1/trade",
        "description": "直接交易下单入口",
    },
    {
        "id": "trade_api_dispatch",
        "type": "api",
        "path": "/v1/trade/dispatch",
        "description": "统一调度入口，operation=trade",
    },
    {
        "id": "trade_api_batch",
        "type": "api",
        "path": "/v1/trade/batch",
        "description": "批量交易入口",
    },
    {
        "id": "signal_api_execute_trade",
        "type": "api",
        "path": "/v1/trade/from-signal",
        "description": "按 signal_id 从交易模块触发交易",
    },
    {
        "id": "signal_runtime_auto_trade",
        "type": "event",
        "path": "SignalRuntime -> TradeExecutor -> dispatch_operation('trade')",
        "description": "信号确认后的自动交易链路",
    },
]


def _enum_or_raw(value: Any) -> str:
    return getattr(value, "value", value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/health", summary="获取系统健康报告")
async def get_health_status(hours: int = 24) -> Dict[str, Any]:
    try:
        return get_runtime_read_model().health_report(hours=hours)
    except Exception as exc:
        logger.error("Failed to generate health report: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/health/live", summary="存活探针")
async def health_live() -> Dict[str, Any]:
    return {"status": "alive", "timestamp": _utc_now()}


@router.get("/health/ready", summary="就绪探针")
async def health_ready() -> Dict[str, Any]:
    try:
        startup = get_startup_status()
        if not startup.get("ready"):
            raise HTTPException(
                status_code=503,
                detail={
                    "status": "not_ready",
                    "phase": startup.get("phase", "unknown"),
                    "error": startup.get("last_error"),
                },
            )

        checks: Dict[str, str] = {}

        try:
            queue_stats = get_ingestor().queue_stats()
            writer_alive = queue_stats.get("threads", {}).get("writer_alive", False)
            checks["storage_writer"] = "ok" if writer_alive else "degraded"
        except Exception:
            checks["storage_writer"] = "error"

        try:
            perf = get_indicator_manager().get_performance_stats()
            checks["indicator_engine"] = (
                "ok" if perf.get("event_loop_running") else "degraded"
            )
        except Exception:
            checks["indicator_engine"] = "error"

        failed_checks = [
            name for name, status in checks.items() if status in {"degraded", "error"}
        ]
        if failed_checks:
            raise HTTPException(
                status_code=503,
                detail={
                    "status": "not_ready",
                    "failed_checks": failed_checks,
                    "checks": checks,
                },
            )

        return {
            "status": "ready",
            "checks": checks,
            "startup_phase": startup.get("phase"),
            "timestamp": _utc_now(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"status": "error", "error": str(exc)},
        ) from exc


@router.get("/performance", summary="获取指标性能统计")
async def get_performance_stats() -> Dict[str, Any]:
    try:
        return get_indicator_manager().get_performance_stats()
    except Exception as exc:
        logger.error("Failed to get performance stats: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/events", summary="获取事件存储统计")
async def get_event_stats() -> Dict[str, Any]:
    try:
        return get_indicator_manager().event_store.get_stats()
    except Exception as exc:
        logger.error("Failed to get event stats: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/queues", summary="获取队列状态")
async def get_queue_stats() -> ApiResponse[Dict[str, Any]]:
    try:
        return ApiResponse.success_response(get_ingestor().queue_stats() or {})
    except Exception as exc:
        logger.error("Failed to get queue stats: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/metrics/{component}/{metric_name}", summary="获取最近指标数据")
async def get_metrics(
    component: str,
    metric_name: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    try:
        health_monitor = get_health_monitor_instance()
        return health_monitor.get_recent_metrics(component, metric_name, limit)
    except Exception as exc:
        logger.error(
            "Failed to get metrics for %s.%s: %s",
            component,
            metric_name,
            exc,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/consistency/check", summary="手动触发一致性检查")
async def trigger_consistency_check() -> Dict[str, Any]:
    try:
        get_indicator_manager().trigger_consistency_check()
        return {"status": "success", "message": "Consistency check triggered"}
    except Exception as exc:
        logger.error("Failed to trigger consistency check: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/events/reset-failed", summary="重置失败事件")
async def reset_failed_events() -> Dict[str, Any]:
    try:
        reset_count = get_indicator_manager().reset_failed_events()
        return {
            "status": "success",
            "message": f"Reset {reset_count} failed events",
            "reset_count": reset_count,
        }
    except Exception as exc:
        logger.error("Failed to reset failed events: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/events/cleanup", summary="清理旧事件")
async def cleanup_old_events(days_to_keep: int = 7) -> Dict[str, Any]:
    try:
        get_indicator_manager().cleanup_old_events(days_to_keep)
        return {
            "status": "success",
            "message": f"Cleaned up events older than {days_to_keep} days",
        }
    except Exception as exc:
        logger.error("Failed to cleanup old events: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/alerts/resolve/{component}/{metric_name}", summary="手动解决告警")
async def resolve_alert(
    component: str,
    metric_name: str,
    resolved_by: str = "api",
) -> Dict[str, Any]:
    try:
        health_monitor = get_health_monitor_instance()
        success = health_monitor.resolve_alert(component, metric_name, resolved_by)
        if success:
            return {
                "status": "success",
                "message": f"Alert resolved for {component}.{metric_name}",
            }
        return {
            "status": "not_found",
            "message": f"No active alert found for {component}.{metric_name}",
        }
    except Exception as exc:
        logger.error(
            "Failed to resolve alert for %s.%s: %s",
            component,
            metric_name,
            exc,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/system/status", summary="获取系统状态摘要")
async def get_system_status() -> Dict[str, Any]:
    try:
        return get_health_monitor_instance().get_system_status()
    except Exception as exc:
        logger.error("Failed to get system status: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/components", summary="获取监控组件列表")
async def get_monitored_components() -> Dict[str, Any]:
    try:
        monitoring_manager = get_monitoring_manager_instance()
        rows = []
        if monitoring_manager and hasattr(
            monitoring_manager, "list_registered_components"
        ):
            rows = monitoring_manager.list_registered_components()
        return {
            "status": "success",
            "components": rows,
            "count": len(rows),
            "check_interval": getattr(monitoring_manager, "check_interval", None),
        }
    except Exception as exc:
        logger.error("Failed to get monitored components: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/trading/trigger-methods", summary="获取交易触发入口")
async def get_trading_trigger_methods() -> Dict[str, Any]:
    return {
        "status": "success",
        "count": len(TRADE_TRIGGER_METHODS),
        "methods": TRADE_TRIGGER_METHODS,
    }


@router.get("/config/effective", summary="获取当前有效运行配置")
async def get_effective_runtime_config() -> ApiResponse[Dict[str, Any]]:
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
            "indicator_cache_strategy": _enum_or_raw(
                indicator_manager.config.pipeline.cache_strategy
            ),
        }
        return ApiResponse.success_response(snapshot)
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
                "staleness": health_monitor.get_recent_metrics(
                    "economic_calendar",
                    "economic_calendar_staleness",
                    50,
                ),
                "provider_failures": health_monitor.get_recent_metrics(
                    "economic_calendar",
                    "economic_provider_failures",
                    50,
                ),
            },
        }
    except Exception as exc:
        logger.error(
            "Failed to get economic calendar monitoring summary: %s",
            exc,
        )
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
                "data_ingestion": get_ingestor()
                .queue_stats()["threads"]
                .get("ingest_alive", False),
                "indicator_calculation": get_indicator_manager()
                .get_performance_stats()
                .get("event_loop_running", False),
                "economic_calendar": get_economic_calendar_service()
                .stats()
                .get("running"),
                "monitoring": bool(get_monitoring_manager_instance()),
            },
        }
        return status
    except Exception as exc:
        logger.error("Failed to get startup monitoring summary: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/config/reload", summary="手动触发配置热加载")
async def trigger_config_reload(filename: str = "signal.ini") -> Dict[str, Any]:
    try:
        reload_configs()
        manager = get_file_config_manager()
        reloaded = manager.reload(filename)
        if not reloaded:
            raise FileNotFoundError(filename)
        return {"success": True, "reloaded": filename, "cache_cleared": True}
    except Exception as exc:
        logger.error("Config reload failed for %s: %s", filename, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/runtime-tasks", summary="获取运行时任务状态")
async def get_runtime_tasks(
    component: str | None = None,
    task_name: str | None = None,
) -> Dict[str, Any]:
    try:
        return {
            "items": get_runtime_task_status(
                component=component,
                task_name=task_name,
            ),
            "filters": {
                "component": component,
                "task_name": task_name,
            },
        }
    except Exception as exc:
        logger.error("Failed to get runtime task status: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/pending-entries", summary="查询当前挂起入场")
async def get_pending_entries() -> ApiResponse[Dict[str, Any]]:
    try:
        return ApiResponse.success_response(
            get_runtime_read_model().pending_entries_summary()
        )
    except Exception as exc:
        logger.error("Failed to get pending entries: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/pending-entries/{signal_id}/cancel", summary="取消指定挂起入场")
async def cancel_pending_entry(
    signal_id: str,
    reason: str = "api",
) -> ApiResponse[Dict[str, Any]]:
    try:
        cancelled = get_pending_entry_manager().cancel(signal_id, reason=reason)
        return ApiResponse.success_response(
            {
                "cancelled": cancelled,
                "signal_id": signal_id,
                "reason": reason,
            }
        )
    except Exception as exc:
        logger.error("Failed to cancel pending entry %s: %s", signal_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/pending-entries/cancel-by-symbol", summary="按品种取消全部挂起入场")
async def cancel_pending_entries_by_symbol(
    symbol: str,
    reason: str = "api",
) -> ApiResponse[Dict[str, Any]]:
    try:
        count = get_pending_entry_manager().cancel_by_symbol(symbol, reason=reason)
        return ApiResponse.success_response(
            {
                "cancelled_count": count,
                "symbol": symbol,
                "reason": reason,
            }
        )
    except Exception as exc:
        logger.error("Failed to cancel pending entries for %s: %s", symbol, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
