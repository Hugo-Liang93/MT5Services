from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_unified_indicator_manager
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse
from src.indicators.manager import UnifiedIndicatorManager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/indicators", tags=["indicators"])


@router.get("/list", response_model=ApiResponse[List[str]])
async def list_available_indicators(
    manager: UnifiedIndicatorManager = Depends(get_unified_indicator_manager),
) -> ApiResponse[List[str]]:
    try:
        indicators = [info["name"] for info in manager.list_indicators()]
        return ApiResponse.success_response(
            data=indicators,
            metadata={"count": len(indicators), "source": "optimized_indicator_service"},
        )
    except Exception as exc:
        logger.error("Failed to list indicators: %s", exc)
        return ApiResponse.error_response(
            error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
            error_message=f"获取指标列表失败: {str(exc)}",
            suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
            details={"exception_type": type(exc).__name__},
        )


@router.get("/performance/stats", response_model=ApiResponse[Dict[str, Any]])
async def get_performance_stats(
    manager: UnifiedIndicatorManager = Depends(get_unified_indicator_manager),
) -> ApiResponse[Dict[str, Any]]:
    try:
        stats = manager.get_performance_stats()
        return ApiResponse.success_response(
            data=stats,
            metadata={"source": "optimized_indicator_service", "timestamp": datetime.now().isoformat()},
        )
    except Exception as exc:
        logger.error("Failed to get performance stats: %s", exc)
        return ApiResponse.error_response(
            error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
            error_message=f"获取性能统计失败: {str(exc)}",
            suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
            details={"exception_type": type(exc).__name__},
        )


@router.post("/cache/clear", response_model=ApiResponse[Dict[str, int]])
async def clear_cache(
    manager: UnifiedIndicatorManager = Depends(get_unified_indicator_manager),
) -> ApiResponse[Dict[str, int]]:
    try:
        cache_count = manager.clear_cache()
        return ApiResponse.success_response(
            data={"cleared_entries": cache_count},
            metadata={"action": "cache_clear", "timestamp": datetime.now().isoformat()},
        )
    except Exception as exc:
        logger.error("Failed to clear cache: %s", exc)
        return ApiResponse.error_response(
            error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
            error_message=f"清空缓存失败: {str(exc)}",
            suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
            details={"exception_type": type(exc).__name__},
        )


@router.get("/dependency/graph", response_model=ApiResponse[Dict[str, str]])
async def get_dependency_graph(
    format: str = Query("mermaid", description="图形格式: mermaid 或 dot"),
    manager: UnifiedIndicatorManager = Depends(get_unified_indicator_manager),
) -> ApiResponse[Dict[str, str]]:
    try:
        graph = manager.get_dependency_graph(format)
        return ApiResponse.success_response(
            data={"graph": graph, "format": format},
            metadata={"source": "dependency_manager", "format": format, "timestamp": datetime.now().isoformat()},
        )
    except Exception as exc:
        logger.error("Failed to get dependency graph: %s", exc)
        return ApiResponse.error_response(
            error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
            error_message=f"获取依赖关系图失败: {str(exc)}",
            suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
            details={"exception_type": type(exc).__name__, "format": format},
        )
