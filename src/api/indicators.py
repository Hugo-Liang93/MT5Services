"""
优化指标API端点

提供对优化指标模块的访问接口
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Any
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api.deps import get_optimized_indicator_service, get_market_service
from src.api.schemas import ApiResponse
from src.api.error_codes import AIErrorCode, AIErrorAction
from src.core.market_service import MarketDataService
from src.indicators_v2.service import OptimizedIndicatorService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/indicators", tags=["indicators"])


# 请求/响应模型
class IndicatorValue(BaseModel):
    """指标值"""
    name: str = Field(..., description="指标名称")
    value: Dict[str, Any] = Field(..., description="指标值字典")
    timestamp: datetime = Field(..., description="计算时间")
    bar_time: Optional[datetime] = Field(None, description="对应的K线时间")
    cache_hit: bool = Field(False, description="是否来自缓存")
    incremental: bool = Field(False, description="是否使用增量计算")
    compute_time_ms: float = Field(0.0, description="计算耗时（毫秒）")


class IndicatorRequest(BaseModel):
    """指标计算请求"""
    symbol: str = Field(..., description="交易品种")
    timeframe: str = Field(..., description="时间框架")
    indicators: List[str] = Field(..., description="指标名称列表")


class IndicatorResponse(BaseModel):
    """指标计算响应"""
    symbol: str = Field(..., description="交易品种")
    timeframe: str = Field(..., description="时间框架")
    indicators: List[IndicatorValue] = Field(..., description="指标值列表")
    total_compute_time_ms: float = Field(0.0, description="总计算耗时（毫秒）")


@router.get("/list", response_model=ApiResponse[List[str]])
async def list_available_indicators(
    service: OptimizedIndicatorService = Depends(get_optimized_indicator_service)
) -> ApiResponse[List[str]]:
    """
    获取可用的指标列表
    
    Returns:
        可用的指标名称列表
    """
    try:
        # 从服务获取任务列表（需要扩展服务接口）
        # 暂时返回示例列表
        indicators = ["sma20", "ema50", "rsi14", "macd", "boll20", "atr14"]
        
        return ApiResponse.success_response(
            data=indicators,
            metadata={
                "count": len(indicators),
                "source": "optimized_indicator_service"
            }
        )
    except Exception as e:
        logger.error(f"Failed to list indicators: {e}")
        return ApiResponse.error_response(
            error_code=AIErrorCode.INTERNAL_ERROR,
            error_message=f"获取指标列表失败: {str(e)}",
            suggested_action=AIErrorAction.RETRY_LATER,
            details={"exception_type": type(e).__name__}
        )


@router.get("/{symbol}/{timeframe}", response_model=ApiResponse[Dict[str, Dict[str, Any]]])
async def get_indicators(
    symbol: str,
    timeframe: str,
    service: OptimizedIndicatorService = Depends(get_optimized_indicator_service)
) -> ApiResponse[Dict[str, Dict[str, Any]]]:
    """
    获取指定品种和时间框架的所有指标
    
    Args:
        symbol: 交易品种
        timeframe: 时间框架
        
    Returns:
        指标名称到值的映射
    """
    try:
        indicators = service.get_all_indicators(symbol, timeframe)
        
        if not indicators:
            return ApiResponse.error_response(
                error_code=AIErrorCode.DATA_NOT_AVAILABLE,
                error_message=f"没有找到 {symbol}/{timeframe} 的指标数据",
                suggested_action=AIErrorAction.USE_FALLBACK_DATA,
                details={"symbol": symbol, "timeframe": timeframe}
            )
        
        return ApiResponse.success_response(
            data=indicators,
            metadata={
                "symbol": symbol,
                "timeframe": timeframe,
                "count": len(indicators),
                "source": "optimized_indicator_service"
            }
        )
    except Exception as e:
        logger.error(f"Failed to get indicators for {symbol}/{timeframe}: {e}")
        return ApiResponse.error_response(
            error_code=AIErrorCode.INTERNAL_ERROR,
            error_message=f"获取指标数据失败: {str(e)}",
            suggested_action=AIErrorAction.RETRY_LATER,
            details={
                "exception_type": type(e).__name__,
                "symbol": symbol,
                "timeframe": timeframe
            }
        )


@router.get("/{symbol}/{timeframe}/{indicator_name}", response_model=ApiResponse[Dict[str, Any]])
async def get_indicator(
    symbol: str,
    timeframe: str,
    indicator_name: str,
    service: OptimizedIndicatorService = Depends(get_optimized_indicator_service)
) -> ApiResponse[Dict[str, Any]]:
    """
    获取单个指标值
    
    Args:
        symbol: 交易品种
        timeframe: 时间框架
        indicator_name: 指标名称
        
    Returns:
        指标值字典
    """
    try:
        indicator_value = service.get_indicator(symbol, timeframe, indicator_name)
        
        if indicator_value is None:
            return ApiResponse.error_response(
                error_code=AIErrorCode.DATA_NOT_AVAILABLE,
                error_message=f"没有找到 {symbol}/{timeframe}/{indicator_name} 的指标数据",
                suggested_action=AIErrorAction.USE_FALLBACK_DATA,
                details={
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "indicator_name": indicator_name
                }
            )
        
        return ApiResponse.success_response(
            data=indicator_value,
            metadata={
                "symbol": symbol,
                "timeframe": timeframe,
                "indicator_name": indicator_name,
                "source": "optimized_indicator_service"
            }
        )
    except Exception as e:
        logger.error(f"Failed to get indicator {indicator_name} for {symbol}/{timeframe}: {e}")
        return ApiResponse.error_response(
            error_code=AIErrorCode.INTERNAL_ERROR,
            error_message=f"获取指标数据失败: {str(e)}",
            suggested_action=AIErrorAction.RETRY_LATER,
            details={
                "exception_type": type(e).__name__,
                "symbol": symbol,
                "timeframe": timeframe,
                "indicator_name": indicator_name
            }
        )


@router.post("/compute", response_model=ApiResponse[Dict[str, Dict[str, Any]]])
async def compute_indicators(
    request: IndicatorRequest,
    service: OptimizedIndicatorService = Depends(get_optimized_indicator_service),
    market_service: MarketDataService = Depends(get_market_service)
) -> ApiResponse[Dict[str, Dict[str, Any]]]:
    """
    按需计算指标（实时计算）
    
    Args:
        request: 指标计算请求
        
    Returns:
        计算结果字典
    """
    try:
        # 验证品种是否存在
        symbols = market_service.list_symbols()
        if request.symbol not in symbols:
            return ApiResponse.error_response(
                error_code=AIErrorCode.MT5_SYMBOL_NOT_FOUND,
                error_message=f"交易品种 '{request.symbol}' 不存在",
                suggested_action=AIErrorAction.USE_DIFFERENT_SYMBOL,
                details={"symbol": request.symbol, "available_symbols": symbols}
            )
        
        # 按需计算指标
        results = service.compute_on_demand(
            symbol=request.symbol,
            timeframe=request.timeframe,
            indicator_names=request.indicators
        )
        
        if not results:
            return ApiResponse.error_response(
                error_code=AIErrorCode.DATA_NOT_AVAILABLE,
                error_message=f"无法计算 {request.symbol}/{request.timeframe} 的指标",
                suggested_action=AIErrorAction.USE_FALLBACK_DATA,
                details={
                    "symbol": request.symbol,
                    "timeframe": request.timeframe,
                    "indicators": request.indicators
                }
            )
        
        return ApiResponse.success_response(
            data=results,
            metadata={
                "symbol": request.symbol,
                "timeframe": request.timeframe,
                "indicators_requested": len(request.indicators),
                "indicators_computed": len(results),
                "source": "optimized_indicator_service_on_demand"
            }
        )
    except Exception as e:
        logger.error(f"Failed to compute indicators: {e}")
        return ApiResponse.error_response(
            error_code=AIErrorCode.INTERNAL_ERROR,
            error_message=f"计算指标失败: {str(e)}",
            suggested_action=AIErrorAction.RETRY_LATER,
            details={
                "exception_type": type(e).__name__,
                "symbol": request.symbol,
                "timeframe": request.timeframe,
                "indicators": request.indicators
            }
        )


@router.get("/performance/stats", response_model=ApiResponse[Dict[str, Any]])
async def get_performance_stats(
    service: OptimizedIndicatorService = Depends(get_optimized_indicator_service)
) -> ApiResponse[Dict[str, Any]]:
    """
    获取优化指标服务的性能统计
    
    Returns:
        性能统计信息
    """
    try:
        stats = service.get_performance_stats()
        
        return ApiResponse.success_response(
            data=stats,
            metadata={
                "source": "optimized_indicator_service",
                "timestamp": datetime.now().isoformat()
            }
        )
    except Exception as e:
        logger.error(f"Failed to get performance stats: {e}")
        return ApiResponse.error_response(
            error_code=AIErrorCode.INTERNAL_ERROR,
            error_message=f"获取性能统计失败: {str(e)}",
            suggested_action=AIErrorAction.RETRY_LATER,
            details={"exception_type": type(e).__name__}
        )


@router.post("/cache/clear", response_model=ApiResponse[Dict[str, int]])
async def clear_cache(
    service: OptimizedIndicatorService = Depends(get_optimized_indicator_service)
) -> ApiResponse[Dict[str, int]]:
    """
    清空指标缓存
    
    Returns:
        清除的缓存项数量
    """
    try:
        cache_count = service.clear_cache()
        
        return ApiResponse.success_response(
            data={"cleared_entries": cache_count},
            metadata={
                "action": "cache_clear",
                "timestamp": datetime.now().isoformat()
            }
        )
    except Exception as e:
        logger.error(f"Failed to clear cache: {e}")
        return ApiResponse.error_response(
            error_code=AIErrorCode.INTERNAL_ERROR,
            error_message=f"清空缓存失败: {str(e)}",
            suggested_action=AIErrorAction.RETRY_LATER,
            details={"exception_type": type(e).__name__}
        )


@router.get("/dependency/graph", response_model=ApiResponse[Dict[str, str]])
async def get_dependency_graph(
    format: str = Query("mermaid", description="图形格式: mermaid 或 dot"),
    service: OptimizedIndicatorService = Depends(get_optimized_indicator_service)
) -> ApiResponse[Dict[str, str]]:
    """
    获取指标依赖关系图
    
    Args:
        format: 图形格式（mermaid 或 dot）
        
    Returns:
        依赖关系图
    """
    try:
        # 需要从依赖管理器获取图
        from src.indicators_v2.engine.dependency_manager import get_global_dependency_manager
        
        dependency_manager = get_global_dependency_manager()
        
        if format.lower() == "dot":
            graph = dependency_manager.visualize("dot")
        else:
            graph = dependency_manager.visualize("mermaid")
        
        return ApiResponse.success_response(
            data={"graph": graph, "format": format},
            metadata={
                "source": "dependency_manager",
                "format": format,
                "timestamp": datetime.now().isoformat()
            }
        )
    except Exception as e:
        logger.error(f"Failed to get dependency graph: {e}")
        return ApiResponse.error_response(
            error_code=AIErrorCode.INTERNAL_ERROR,
            error_message=f"获取依赖关系图失败: {str(e)}",
            suggested_action=AIErrorAction.RETRY_LATER,
            details={"exception_type": type(e).__name__, "format": format}
        )