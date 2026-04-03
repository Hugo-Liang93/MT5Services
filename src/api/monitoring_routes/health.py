from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from src.api.deps import (
    get_health_monitor_instance,
    get_indicator_manager,
    get_ingestor,
    get_monitoring_manager_instance,
    get_runtime_read_model,
    get_startup_status,
)
from src.api.schemas import ApiResponse
from .view_models import (
    AlertResolutionView,
    FailedEventsResetView,
    HealthLiveView,
    MonitoredComponentsView,
    ReadyProbeView,
    RuntimeActionView,
    TradingTriggerMethodsView,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/monitoring", tags=["monitoring"])


TRADE_TRIGGER_METHODS = [
    {"id": "trade_api_direct", "type": "api", "path": "/v1/trade", "description": "直接交易下单入口"},
    {"id": "trade_api_dispatch", "type": "api", "path": "/v1/trade/dispatch", "description": "统一调度入口，operation=trade"},
    {"id": "trade_api_batch", "type": "api", "path": "/v1/trade/batch", "description": "批量交易入口"},
    {"id": "signal_api_execute_trade", "type": "api", "path": "/v1/trade/from-signal", "description": "按 signal_id 从交易模块触发交易"},
    {"id": "signal_runtime_auto_trade", "type": "event", "path": "SignalRuntime -> TradeExecutor -> dispatch_operation('trade')", "description": "信号确认后的自动交易链路"},
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/health", summary="获取系统健康报告")
async def get_health_status(hours: int = 24) -> Dict[str, Any]:
    try:
        return get_runtime_read_model().health_report(hours=hours)
    except Exception as exc:
        logger.error("Failed to generate health report: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/health/live", response_model=HealthLiveView, summary="存活探针")
async def health_live() -> Dict[str, Any]:
    return HealthLiveView(status="alive", timestamp=_utc_now()).model_dump()


@router.get("/health/ready", response_model=ReadyProbeView, summary="就绪探针")
async def health_ready() -> Dict[str, Any]:
    try:
        startup = get_startup_status()
        if not startup.get("ready"):
            raise HTTPException(
                status_code=503,
                detail={"status": "not_ready", "phase": startup.get("phase", "unknown"), "error": startup.get("last_error")},
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
            checks["indicator_engine"] = "ok" if perf.get("event_loop_running") else "degraded"
        except Exception:
            checks["indicator_engine"] = "error"
        failed_checks = [name for name, status in checks.items() if status in {"degraded", "error"}]
        if failed_checks:
            raise HTTPException(
                status_code=503,
                detail={"status": "not_ready", "failed_checks": failed_checks, "checks": checks},
            )
        return ReadyProbeView(
            status="ready",
            checks=checks,
            startup_phase=startup.get("phase"),
            timestamp=_utc_now(),
        ).model_dump()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"status": "error", "error": str(exc)}) from exc


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
async def get_metrics(component: str, metric_name: str, limit: int = 100) -> List[Dict[str, Any]]:
    try:
        return get_health_monitor_instance().get_recent_metrics(component, metric_name, limit)
    except Exception as exc:
        logger.error("Failed to get metrics for %s.%s: %s", component, metric_name, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/consistency/check", response_model=RuntimeActionView, summary="手动触发一致性检查")
async def trigger_consistency_check() -> Dict[str, Any]:
    try:
        get_indicator_manager().trigger_consistency_check()
        return RuntimeActionView(status="success", message="Consistency check triggered").model_dump()
    except Exception as exc:
        logger.error("Failed to trigger consistency check: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/events/reset-failed", response_model=FailedEventsResetView, summary="重置失败事件")
async def reset_failed_events() -> Dict[str, Any]:
    try:
        reset_count = get_indicator_manager().reset_failed_events()
        return FailedEventsResetView(
            status="success",
            message=f"Reset {reset_count} failed events",
            reset_count=reset_count,
        ).model_dump()
    except Exception as exc:
        logger.error("Failed to reset failed events: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/events/cleanup", response_model=RuntimeActionView, summary="清理旧事件")
async def cleanup_old_events(days_to_keep: int = 7) -> Dict[str, Any]:
    try:
        get_indicator_manager().cleanup_old_events(days_to_keep)
        return RuntimeActionView(
            status="success",
            message=f"Cleaned up events older than {days_to_keep} days",
        ).model_dump()
    except Exception as exc:
        logger.error("Failed to cleanup old events: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/alerts/resolve/{component}/{metric_name}", response_model=AlertResolutionView, summary="手动解决告警")
async def resolve_alert(component: str, metric_name: str, resolved_by: str = "api") -> Dict[str, Any]:
    try:
        success = get_health_monitor_instance().resolve_alert(component, metric_name, resolved_by)
        if success:
            return AlertResolutionView(status="success", message=f"Alert resolved for {component}.{metric_name}").model_dump()
        return AlertResolutionView(status="not_found", message=f"No active alert found for {component}.{metric_name}").model_dump()
    except Exception as exc:
        logger.error("Failed to resolve alert for %s.%s: %s", component, metric_name, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/system/status", summary="获取系统状态摘要")
async def get_system_status() -> Dict[str, Any]:
    try:
        return get_health_monitor_instance().get_system_status()
    except Exception as exc:
        logger.error("Failed to get system status: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/components", response_model=MonitoredComponentsView, summary="获取监控组件列表")
async def get_monitored_components() -> Dict[str, Any]:
    try:
        monitoring_manager = get_monitoring_manager_instance()
        rows = []
        if monitoring_manager and hasattr(monitoring_manager, "list_registered_components"):
            rows = monitoring_manager.list_registered_components()
        return MonitoredComponentsView(
            status="success",
            components=rows,
            count=len(rows),
            check_interval=getattr(monitoring_manager, "check_interval", None),
        ).model_dump()
    except Exception as exc:
        logger.error("Failed to get monitored components: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/trading/trigger-methods", response_model=TradingTriggerMethodsView, summary="获取交易触发入口")
async def get_trading_trigger_methods() -> Dict[str, Any]:
    return TradingTriggerMethodsView(status="success", count=len(TRADE_TRIGGER_METHODS), methods=TRADE_TRIGGER_METHODS).model_dump()
