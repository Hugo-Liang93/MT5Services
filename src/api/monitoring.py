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
)
from src.config import get_effective_config_snapshot

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/monitoring", tags=["monitoring"])


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
            "indicator_cache_strategy": indicator_manager.config.pipeline.cache_strategy.value,
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
