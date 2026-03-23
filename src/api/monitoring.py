"""
监控API：提供系统健康状态、性能指标和事件统计
"""

from fastapi import APIRouter, HTTPException
from typing import Any, Dict, List
import logging

from src.api.schemas import ApiResponse

from src.api.deps import (
    get_economic_calendar_service,
    get_health_monitor_instance,
    get_indicator_manager,
    get_ingestor,
    get_monitoring_manager_instance,
    get_pending_entry_manager,
    get_runtime_task_status,
    get_startup_status,
    get_trading_service,
)
from src.config import get_effective_config_snapshot
from src.config.file_manager import get_file_config_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/monitoring", tags=["monitoring"])

TRADE_TRIGGER_METHODS = [
    {
        "id": "trade_api_direct",
        "type": "api",
        "path": "/trade",
        "description": "直接交易下单入口",
    },
    {
        "id": "trade_api_dispatch",
        "type": "api",
        "path": "/trade/dispatch",
        "description": "统一调度入口（operation=trade）",
    },
    {
        "id": "trade_api_batch",
        "type": "api",
        "path": "/trade/batch",
        "description": "批量交易入口",
    },
    {
        "id": "signal_api_execute_trade",
        "type": "api",
        "path": "/trade/from-signal",
        "description": "按 signal_id 从交易模块触发交易",
    },
    {
        "id": "signal_runtime_auto_trade",
        "type": "event",
        "path": "SignalRuntime -> TradeExecutor -> dispatch_operation('trade')",
        "description": "信号确认后的自动交易链路",
    },
]


def _enum_or_raw(value) -> str:
    return getattr(value, "value", value)


def _runtime_indicator_status(
    event_loop_running: bool,
    failed_computations: int,
    event_stats: Dict[str, Any],
) -> str:
    if not event_loop_running:
        return "critical"
    if event_stats.get("failed", 0) > 0:
        return "critical"
    if failed_computations > 0 or event_stats.get("retrying", 0) > 0:
        return "warning"
    return "healthy"


def _build_storage_runtime_summary(queue_stats: Dict[str, Any]) -> Dict[str, Any]:
    summary = dict(queue_stats.get("summary", {}) or {})
    queues = dict(queue_stats.get("queues", {}) or {})
    threads = dict(queue_stats.get("threads", {}) or {})
    worst_queue = None

    for name, queue in queues.items():
        queue_status = str(queue.get("status", "normal"))
        queue_score = {"normal": 0, "high": 1, "critical": 2, "full": 3}.get(queue_status, 0)
        if worst_queue is None or queue_score > worst_queue["score"]:
            worst_queue = {
                "name": name,
                "score": queue_score,
                "status": queue_status,
                "utilization_pct": queue.get("utilization_pct", 0.0),
                "pending": queue.get("pending", 0),
            }

    if not threads.get("writer_alive", False):
        status = "critical"
    elif summary.get("full", 0) > 0:
        status = "critical"
    elif summary.get("critical", 0) > 0 or summary.get("high", 0) > 0:
        status = "warning"
    else:
        status = "healthy"

    return {
        "status": status,
        "threads": threads,
        "summary": summary,
        "worst_queue": worst_queue,
    }


def _build_runtime_health_summary(indicator_stats: Dict[str, Any]) -> Dict[str, Any]:
    event_stats = dict(indicator_stats.get("event_store", {}) or {})
    pipeline_stats = dict(indicator_stats.get("pipeline", {}) or {})
    event_loop_running = indicator_stats.get("event_loop_running", False)
    failed_computations = indicator_stats.get("failed_computations", 0)

    return {
        "status": _runtime_indicator_status(
            event_loop_running,
            failed_computations,
            event_stats,
        ),
        "mode": indicator_stats.get("mode"),
        "event_loop_running": event_loop_running,
        "last_reconcile_at": indicator_stats.get("last_reconcile_at"),
        "computations": {
            "total": indicator_stats.get("total_computations", 0),
            "failed": failed_computations,
            "success_rate": indicator_stats.get("success_rate", 0),
            "cached": indicator_stats.get("cached_computations", 0),
            "incremental": indicator_stats.get("incremental_computations", 0),
            "parallel": indicator_stats.get("parallel_computations", 0),
        },
        "events": {
            "pending": event_stats.get("pending", 0),
            "processing": event_stats.get("processing", 0),
            "completed": event_stats.get("completed", 0),
            "skipped": event_stats.get("skipped", 0),
            "failed": event_stats.get("failed", 0),
            "retrying": event_stats.get("retrying", 0),
            "total_retries": event_stats.get("total_retries", 0),
            "outcome_counts": dict(event_stats.get("outcome_counts", {}) or {}),
            "recent_skips": list(event_stats.get("recent_skips", []) or []),
            "recent_retryable_errors": list(
                event_stats.get("recent_retryable_errors", []) or []
            ),
            "recent_errors": list(event_stats.get("recent_errors", []) or []),
        },
        "cache": {
            "hits": indicator_stats.get("cache_hits", 0),
            "misses": indicator_stats.get("cache_misses", 0),
            "snapshot": dict(pipeline_stats.get("cache", {}) or {}),
        },
        "results": dict(indicator_stats.get("results", {}) or {}),
        "config": dict(indicator_stats.get("config", {}) or {}),
        "timestamp": indicator_stats.get("timestamp"),
    }


def _build_runtime_trading_summary(trading_stats: Dict[str, Any]) -> Dict[str, Any]:
    summary_rows = list(trading_stats.get("summary", []) or [])
    accounts = list(trading_stats.get("accounts", []) or [])
    recent = list(trading_stats.get("recent", []) or [])
    active_account_alias = trading_stats.get("active_account_alias")
    failed = sum(int(row.get("count", 0)) for row in summary_rows if row.get("status") == "failed")
    if failed > 0:
        status = "warning"
    else:
        status = "healthy"
    daily = dict(trading_stats.get("daily", {}) or {})
    risk_summary = dict(daily.get("risk", {}) or {})
    coordination_issues = []
    if daily.get("failed", 0) and daily.get("success", 0) == 0:
        coordination_issues.append("当日交易全部失败，建议检查风控与交易连接模块")
    if failed > 0:
        coordination_issues.append("检测到交易失败记录，建议检查交易调度与账户状态同步")
    if int(risk_summary.get("blocked", 0)) > 0:
        coordination_issues.append("存在风控拦截交易，建议复核风控阈值、经济事件窗口与仓位限制")
    return {
        "status": status,
        "active_account_alias": active_account_alias,
        "accounts": accounts,
        "daily": daily,
        "risk": risk_summary,
        "coordination_issues": coordination_issues,
        "summary": summary_rows,
        "recent": recent,
    }


@router.get("/health", summary="获取系统健康状态")
async def get_health_status(hours: int = 24) -> Dict[str, Any]:
    """
    获取系统健康状态报告
    
    Args:
        hours: 报告时间范围（小时）
    
    Returns:
        健康状态报告
    """
    try:
        health_monitor = get_health_monitor_instance()
        report = health_monitor.generate_report(hours)
        indicator_manager = get_indicator_manager()
        queue_stats = get_ingestor().queue_stats()
        report["runtime"] = {
            "storage": _build_storage_runtime_summary(queue_stats),
            "indicators": _build_runtime_health_summary(
                indicator_manager.get_performance_stats()
            ),
            "trading": _build_runtime_trading_summary(
                get_trading_service().monitoring_summary(hours=hours)
            ),
        }
        return report
    except Exception as e:
        logger.error(f"Failed to generate health report: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health/live", summary="存活探针（Liveness Probe）")
async def health_live() -> Dict[str, Any]:
    """轻量级存活检查：只要进程能响应 HTTP 请求即返回 200。

    适用于 Kubernetes/Docker liveness probe。
    不检查 MT5 连接或数据库，避免因外部依赖临时故障导致进程被误重启。
    """
    return {"status": "alive", "timestamp": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())}


@router.get("/health/ready", summary="就绪探针（Readiness Probe）")
async def health_ready() -> Dict[str, Any]:
    """就绪检查：验证系统已完成启动且核心组件正常运行。

    适用于 Kubernetes/Docker readiness probe。
    任何关键组件未就绪时返回 503，让负载均衡器暂停向本实例转发流量。

    检查项：
    - 启动流程已完成（startup_status.ready == True）
    - StorageWriter 写入线程存活
    - 指标引擎事件循环运行中
    """
    try:
        startup = get_startup_status()
        if not startup.get("ready"):
            phase = startup.get("phase", "unknown")
            raise HTTPException(
                status_code=503,
                detail={"status": "not_ready", "phase": phase, "error": startup.get("last_error")},
            )

        checks: Dict[str, str] = {}

        # 检查 StorageWriter
        try:
            ingestor = get_ingestor()
            queue_stats = ingestor.queue_stats()
            writer_alive = queue_stats.get("threads", {}).get("writer_alive", False)
            checks["storage_writer"] = "ok" if writer_alive else "degraded"
        except Exception:
            checks["storage_writer"] = "error"

        # 检查指标引擎
        try:
            indicator_manager = get_indicator_manager()
            perf = indicator_manager.get_performance_stats()
            checks["indicator_engine"] = "ok" if perf.get("event_loop_running") else "degraded"
        except Exception:
            checks["indicator_engine"] = "error"

        # 若有任何 error 状态则返回 503
        failed = [k for k, v in checks.items() if v == "error"]
        if failed:
            raise HTTPException(
                status_code=503,
                detail={"status": "not_ready", "failed_checks": failed, "checks": checks},
            )

        overall = "degraded" if any(v == "degraded" for v in checks.values()) else "ready"
        return {
            "status": overall,
            "checks": checks,
            "startup_phase": startup.get("phase"),
            "timestamp": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"status": "error", "error": str(exc)}) from exc


@router.get("/performance", summary="获取性能指标")
async def get_performance_stats() -> Dict[str, Any]:
    """
    获取性能指标
    
    Returns:
        性能指标数据
    """
    try:
        indicator_manager = get_indicator_manager()
        stats = indicator_manager.get_performance_stats()
        return stats
    except Exception as e:
        logger.error(f"Failed to get performance stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/events", summary="获取事件统计")
async def get_event_stats() -> Dict[str, Any]:
    """
    获取事件存储统计
    
    Returns:
        事件统计信息
    """
    try:
        indicator_manager = get_indicator_manager()
        event_store = indicator_manager.event_store
        stats = event_store.get_stats()
        return stats
    except Exception as e:
        logger.error(f"Failed to get event stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/queues", summary="获取队列状态")
async def get_queue_stats() -> ApiResponse[Dict[str, Any]]:
    """获取队列状态"""
    try:
        ingestor = get_ingestor()
        stats = ingestor.queue_stats()
        return ApiResponse.success_response(data=stats or {})
    except Exception as e:
        logger.error(f"Failed to get queue stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/metrics/{component}/{metric_name}", summary="获取特定指标数据")
async def get_metrics(
    component: str, 
    metric_name: str, 
    limit: int = 100
) -> List[Dict[str, Any]]:
    """
    获取特定指标的最近数据
    
    Args:
        component: 组件名称
        metric_name: 指标名称
        limit: 返回数据点数量
    
    Returns:
        指标数据列表
    """
    try:
        health_monitor = get_health_monitor_instance()
        metrics = health_monitor.get_recent_metrics(component, metric_name, limit)
        return metrics
    except Exception as e:
        logger.error(f"Failed to get metrics for {component}.{metric_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/consistency/check", summary="手动触发一致性检查")
async def trigger_consistency_check() -> Dict[str, Any]:
    """
    手动触发指标缓存一致性检查
    
    Returns:
        操作结果
    """
    try:
        indicator_manager = get_indicator_manager()
        indicator_manager.trigger_consistency_check()
        return {"status": "success", "message": "Consistency check triggered"}
    except Exception as e:
        logger.error(f"Failed to trigger consistency check: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/events/reset-failed", summary="重置失败事件")
async def reset_failed_events() -> Dict[str, Any]:
    """
    重置失败事件，使其可以重试
    
    Returns:
        操作结果
    """
    try:
        indicator_manager = get_indicator_manager()
        reset_count = indicator_manager.reset_failed_events()
        return {
            "status": "success", 
            "message": f"Reset {reset_count} failed events",
            "reset_count": reset_count
        }
    except Exception as e:
        logger.error(f"Failed to reset failed events: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/events/cleanup", summary="清理旧事件")
async def cleanup_old_events(days_to_keep: int = 7) -> Dict[str, Any]:
    """
    清理指定天数之前的旧事件
    
    Args:
        days_to_keep: 保留天数
    
    Returns:
        操作结果
    """
    try:
        indicator_manager = get_indicator_manager()
        indicator_manager.cleanup_old_events(days_to_keep)
        return {
            "status": "success", 
            "message": f"Cleaned up events older than {days_to_keep} days"
        }
    except Exception as e:
        logger.error(f"Failed to cleanup old events: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/alerts/resolve/{component}/{metric_name}", summary="解决告警")
async def resolve_alert(
    component: str, 
    metric_name: str,
    resolved_by: str = "api"
) -> Dict[str, Any]:
    """
    手动解决告警
    
    Args:
        component: 组件名称
        metric_name: 指标名称
        resolved_by: 解决者标识
    
    Returns:
        操作结果
    """
    try:
        health_monitor = get_health_monitor_instance()
        success = health_monitor.resolve_alert(component, metric_name, resolved_by)
        
        if success:
            return {
                "status": "success", 
                "message": f"Alert resolved for {component}.{metric_name}"
            }
        else:
            return {
                "status": "not_found", 
                "message": f"No active alert found for {component}.{metric_name}"
            }
    except Exception as e:
        logger.error(f"Failed to resolve alert for {component}.{metric_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/system/status", summary="获取系统状态摘要")
async def get_system_status() -> Dict[str, Any]:
    """
    获取系统状态摘要
    
    Returns:
        系统状态信息
    """
    try:
        health_monitor = get_health_monitor_instance()
        status = health_monitor.get_system_status()
        return status
    except Exception as e:
        logger.error(f"Failed to get system status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/components", summary="获取监控组件列表")
async def get_monitored_components() -> Dict[str, Any]:
    """
    获取当前监控的组件列表
    
    Returns:
        组件列表
    """
    try:
        monitoring_manager = get_monitoring_manager_instance()
        rows = []
        if monitoring_manager and hasattr(monitoring_manager, "list_registered_components"):
            rows = monitoring_manager.list_registered_components()
        legacy_names = [item.get("name") for item in rows]
        if "trading" not in legacy_names:
            rows.append({"name": "trading", "methods": ["monitoring_summary"], "enabled": True, "source": "api"})
        return {
            "status": "success",
            "components": rows,
            "count": len(rows),
            "check_interval": getattr(monitoring_manager, "check_interval", None),
        }
    except Exception as e:
        logger.error(f"Failed to get monitored components: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/trading/trigger-methods", summary="Get trade trigger methods")
async def get_trading_trigger_methods() -> Dict[str, Any]:
    """返回当前系统触发交易的全部入口，便于排查信号到交易链路。"""
    return {
        "status": "success",
        "count": len(TRADE_TRIGGER_METHODS),
        "methods": TRADE_TRIGGER_METHODS,
    }


@router.get("/config/effective", summary="Get effective runtime config")
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
        return ApiResponse.success_response(data=snapshot)
    except Exception as e:
        logger.error(f"Failed to get effective runtime config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/economic-calendar", summary="Get economic calendar monitoring summary")
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
    except Exception as e:
        logger.error(f"Failed to get economic calendar monitoring summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/trading", summary="Get trading monitoring summary")
async def get_trading_monitoring(hours: int = 24) -> Dict[str, Any]:
    try:
        service = get_trading_service()
        return service.monitoring_summary(hours=hours)
    except Exception as e:
        logger.error(f"Failed to get trading monitoring summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/startup", summary="Get startup phase and timing summary")
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
    except Exception as e:
        logger.error(f"Failed to get startup monitoring summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/config/reload", summary="手动触发配置热加载")
async def trigger_config_reload(filename: str = "signal.ini") -> Dict[str, Any]:
    """触发指定配置文件的热加载，无需重启服务。

    ``filename`` 参数应为 config/ 目录下的文件名，例如 ``signal.ini``。
    该文件将被重新读取，并通过 ``_notify_config_change`` 通知所有已注册组件。
    """
    try:
        mgr = get_file_config_manager()
        mgr._load_config(filename)
        mgr._notify_config_change(filename)
        return {"success": True, "reloaded": filename}
    except Exception as exc:
        logger.error("Config reload failed for %s: %s", filename, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/runtime-tasks", summary="Get persisted runtime task status")
async def get_runtime_tasks(component: str | None = None, task_name: str | None = None) -> Dict[str, Any]:
    try:
        return {
            "items": get_runtime_task_status(component=component, task_name=task_name),
            "filters": {
                "component": component,
                "task_name": task_name,
            },
        }
    except Exception as e:
        logger.error(f"Failed to get runtime task status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# Pending Entry（价格确认入场）
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/pending-entries", summary="查询所有挂起的入场意图")
async def get_pending_entries() -> ApiResponse[Dict[str, Any]]:
    """返回当前所有挂起的 PendingEntry 及统计数据。"""
    try:
        mgr = get_pending_entry_manager()
        return ApiResponse.success_response(data=mgr.status())
    except Exception as e:
        logger.error("Failed to get pending entries: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pending-entries/{signal_id}/cancel", summary="取消指定的挂起入场")
async def cancel_pending_entry(signal_id: str, reason: str = "api") -> ApiResponse[Dict[str, Any]]:
    """取消指定 signal_id 的挂起入场意图。"""
    try:
        mgr = get_pending_entry_manager()
        cancelled = mgr.cancel(signal_id, reason=reason)
        return ApiResponse.success_response(
            data={"cancelled": cancelled, "signal_id": signal_id, "reason": reason},
        )
    except Exception as e:
        logger.error("Failed to cancel pending entry %s: %s", signal_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pending-entries/cancel-by-symbol", summary="按品种取消所有挂起入场")
async def cancel_pending_entries_by_symbol(
    symbol: str,
    reason: str = "api",
) -> ApiResponse[Dict[str, Any]]:
    """取消指定品种的所有挂起入场意图。"""
    try:
        mgr = get_pending_entry_manager()
        count = mgr.cancel_by_symbol(symbol, reason=reason)
        return ApiResponse.success_response(
            data={"cancelled_count": count, "symbol": symbol, "reason": reason},
        )
    except Exception as e:
        logger.error("Failed to cancel pending entries for %s: %s", symbol, e)
        raise HTTPException(status_code=500, detail=str(e))
