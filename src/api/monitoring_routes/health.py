from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List
from collections.abc import Callable
from typing import TypeVar

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
_T = TypeVar("_T")


TRADE_TRIGGER_METHODS = [
    {"id": "trade_api_direct", "type": "api", "path": "/v1/trade", "description": "直接交易下单入口"},
    {"id": "trade_api_dispatch", "type": "api", "path": "/v1/trade/dispatch", "description": "统一调度入口，operation=trade"},
    {"id": "trade_api_batch", "type": "api", "path": "/v1/trade/batch", "description": "批量交易入口"},
    {"id": "signal_api_execute_trade", "type": "api", "path": "/v1/trade/from-signal", "description": "按 signal_id 从交易模块触发交易"},
    {"id": "signal_runtime_auto_trade", "type": "event", "path": "SignalRuntime -> TradeExecutor -> dispatch_operation('trade')", "description": "信号确认后的自动交易链路"},
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_check_call(label: str, fn):
    try:
        return fn(), None
    except (AttributeError, KeyError, RuntimeError, TypeError, ValueError) as exc:
        logger.warning("%s check failed: %s", label, exc)
        return None, str(exc)
    except Exception as exc:
        logger.error("%s check failed with unexpected error: %s", label, exc, exc_info=True)
        return None, str(exc)


def _execute_health_call(
    label: str,
    operation: Callable[[], _T],
    *,
    fallback: _T,
    allow_fallback: bool = False,
) -> _T:
    try:
        return operation()
    except HTTPException:
        raise
    except (AssertionError, AttributeError, KeyError, RuntimeError, TypeError, ValueError) as exc:
        logger.warning("%s failed with expected error: %s", label, exc)
        if allow_fallback:
            return fallback
        raise HTTPException(status_code=500, detail=f"{label} failed: {exc}") from exc
    except Exception as exc:
        logger.error("%s failed with unexpected error", label, exc_info=True)
        if allow_fallback:
            return fallback
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── K8s 探针（不使用 ApiResponse，返回简单结构）────────────────────


@router.get("/health/live", response_model=HealthLiveView, summary="存活探针")
async def health_live() -> Dict[str, Any]:
    return HealthLiveView(status="alive", timestamp=_utc_now()).model_dump()


@router.get("/health/ready", response_model=ReadyProbeView, summary="就绪探针")
async def health_ready() -> Dict[str, Any]:
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

    queue_stats, queue_err = _safe_check_call("storage_writer", get_ingestor().queue_stats)
    if queue_err is None and isinstance(queue_stats, dict):
        thread_stats = dict(queue_stats.get("threads", {}) or {})
        writer_alive = thread_stats.get("writer_alive", False)
        ingest_alive = thread_stats.get("ingest_alive", False)
        checks["storage_writer"] = "ok" if writer_alive else "degraded"
        checks["ingestion"] = "ok" if ingest_alive else "degraded"
    else:
        checks["storage_writer"] = "error"
        checks["ingestion"] = "error"

    perf, perf_err = _safe_check_call("indicator_engine", get_indicator_manager().get_performance_stats)
    if perf_err is None and isinstance(perf, dict):
        checks["indicator_engine"] = "ok" if perf.get("event_loop_running") else "degraded"
    else:
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


# ── 业务端点（统一 ApiResponse 包装）────────────────────────────


@router.get("/health", summary="获取系统健康报告")
async def get_health_status(hours: int = 24) -> ApiResponse[Dict[str, Any]]:
    data = _execute_health_call(
        "health report",
        lambda: get_runtime_read_model().health_report(hours=hours),
        fallback={},
    )
    return ApiResponse.success_response(data)


@router.get("/performance", summary="获取指标性能统计")
async def get_performance_stats() -> ApiResponse[Dict[str, Any]]:
    return ApiResponse.success_response(
        _execute_health_call("indicator performance stats", get_indicator_manager().get_performance_stats, fallback={})
    )


@router.get("/events", summary="获取事件存储统计")
async def get_event_stats() -> ApiResponse[Dict[str, Any]]:
    return ApiResponse.success_response(
        _execute_health_call(
            "indicator event store stats",
            lambda: get_indicator_manager().event_store.get_stats(),
            fallback={},
        )
    )


@router.get("/queues", summary="获取队列状态")
async def get_queue_stats() -> ApiResponse[Dict[str, Any]]:
    return ApiResponse.success_response(
        _execute_health_call("queue stats", lambda: get_ingestor().queue_stats() or {}, fallback={})
    )


@router.get("/metrics/{component}/{metric_name}", summary="获取最近指标数据")
async def get_metrics(component: str, metric_name: str, limit: int = 100) -> ApiResponse[List[Dict[str, Any]]]:
    data = _execute_health_call(
        f"metrics for {component}.{metric_name}",
        lambda: get_health_monitor_instance().get_recent_metrics(component, metric_name, limit),
        fallback=[],
    )
    return ApiResponse.success_response(data)


@router.post("/consistency/check", summary="手动触发一致性检查")
async def trigger_consistency_check() -> ApiResponse[RuntimeActionView]:
    _execute_health_call(
        "consistency check trigger",
        get_indicator_manager().trigger_consistency_check,
        fallback=None,
    )
    return ApiResponse.success_response(
        RuntimeActionView(status="success", message="Consistency check triggered").model_dump()
    )


@router.post("/events/reset-failed", summary="重置失败事件")
async def reset_failed_events() -> ApiResponse[FailedEventsResetView]:
    reset_count = _execute_health_call(
        "failed events reset",
        get_indicator_manager().reset_failed_events,
        fallback=0,
        allow_fallback=True,
    )
    return ApiResponse.success_response(
        FailedEventsResetView(
            status="success",
            message=f"Reset {reset_count} failed events",
            reset_count=reset_count,
        ).model_dump()
    )


@router.post("/events/cleanup", summary="清理旧事件")
async def cleanup_old_events(days_to_keep: int = 7) -> ApiResponse[RuntimeActionView]:
    _execute_health_call(
        "event cleanup",
        lambda: get_indicator_manager().cleanup_old_events(days_to_keep),
        fallback=None,
    )
    return ApiResponse.success_response(
        RuntimeActionView(
            status="success",
            message=f"Cleaned up events older than {days_to_keep} days",
        ).model_dump()
    )


@router.post("/alerts/resolve/{component}/{metric_name}", summary="手动解决告警")
async def resolve_alert(component: str, metric_name: str, resolved_by: str = "api") -> ApiResponse[AlertResolutionView]:
    success = _execute_health_call(
        "alert resolve",
        lambda: get_health_monitor_instance().resolve_alert(component, metric_name, resolved_by),
        fallback=False,
        allow_fallback=True,
    )
    if success:
        view = AlertResolutionView(status="success", message=f"Alert resolved for {component}.{metric_name}")
    else:
        view = AlertResolutionView(status="not_found", message=f"No active alert found for {component}.{metric_name}")
    return ApiResponse.success_response(view.model_dump())


@router.get("/system/status", summary="获取系统状态摘要")
async def get_system_status() -> ApiResponse[Dict[str, Any]]:
    return ApiResponse.success_response(
        _execute_health_call(
            "system status",
            get_health_monitor_instance().get_system_status,
            fallback={},
        )
    )


@router.get("/components", summary="获取监控组件列表")
async def get_monitored_components() -> ApiResponse[MonitoredComponentsView]:
    monitoring_manager = get_monitoring_manager_instance()
    rows: list[dict] = []
    check_interval = None
    if monitoring_manager:
        rows, err = _safe_check_call(
            "monitoring_components",
            monitoring_manager.list_registered_components,
        )
        if err is None and isinstance(rows, list):
            check_interval = getattr(monitoring_manager, "check_interval", None)
        else:
            logger.warning("Failed to load monitoring components: %s", err)
            rows = []

    return ApiResponse.success_response(
        MonitoredComponentsView(
            status="success",
            components=rows,
            count=len(rows),
            check_interval=check_interval,
        ).model_dump()
    )


@router.get("/trading/trigger-methods", summary="获取交易触发入口")
async def get_trading_trigger_methods() -> ApiResponse[TradingTriggerMethodsView]:
    return ApiResponse.success_response(
        TradingTriggerMethodsView(status="success", count=len(TRADE_TRIGGER_METHODS), methods=TRADE_TRIGGER_METHODS).model_dump()
    )
