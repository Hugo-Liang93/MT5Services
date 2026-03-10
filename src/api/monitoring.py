"""
监控API：提供系统健康状态、性能指标和事件统计
"""

from fastapi import APIRouter, HTTPException
from typing import Dict, Any, List
import logging

from src.api.deps_enhanced import (
    get_health_monitor_instance,
    get_indicator_worker,
    get_ingestor,
    get_monitoring_manager_instance
)

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
        indicator_worker = get_indicator_worker()
        stats = indicator_worker.get_performance_stats()
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
        indicator_worker = get_indicator_worker()
        event_store = indicator_worker.event_store
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
        indicator_worker = get_indicator_worker()
        indicator_worker.trigger_consistency_check()
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
        indicator_worker = get_indicator_worker()
        reset_count = indicator_worker.reset_failed_events()
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
        indicator_worker = get_indicator_worker()
        indicator_worker.cleanup_old_events(days_to_keep)
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
            "components": ["data_ingestion", "indicator_calculation", "market_data"],
            "check_interval": monitoring_manager.check_interval
        }
    except Exception as e:
        logger.error(f"Failed to get monitored components: {e}")
        raise HTTPException(status_code=500, detail=str(e))