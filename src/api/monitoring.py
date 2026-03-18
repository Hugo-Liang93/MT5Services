"""
监控API：提供系统健康状态、性能指标和事件统计
"""

from fastapi import APIRouter, HTTPException
from typing import Dict, Any, List
import logging

from src.api.deps import (
    get_economic_calendar_service,
    get_health_monitor_instance,
    get_indicator_manager,
    get_ingestor,
    get_monitoring_manager_instance,
    get_runtime_task_status,
    get_startup_status,
    get_trading_service,
)
from src.config import get_effective_config_snapshot
from src.config.advanced_manager import get_config_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/monitoring", tags=["monitoring"])


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
async def get_queue_stats() -> Dict[str, Any]:
    """
    获取队列状态
    
    Returns:
        队列状态信息
    """
    try:
        ingestor = get_ingestor()
        stats = ingestor.queue_stats()
        return stats
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
        # 这里需要添加获取组件列表的方法
        return {
            "status": "success",
            "components": ["data_ingestion", "indicator_calculation", "market_data", "economic_calendar"],
            "check_interval": monitoring_manager.check_interval
        }
    except Exception as e:
        logger.error(f"Failed to get monitored components: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/config/effective", summary="Get effective runtime config")
async def get_effective_runtime_config() -> Dict[str, Any]:
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
        return snapshot
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
        mgr = get_config_manager()
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
