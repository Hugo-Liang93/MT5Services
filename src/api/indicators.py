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

from src.api.deps import get_unified_indicator_manager, get_market_service
from src.api.schemas import ApiResponse
from src.api.error_codes import AIErrorCode, AIErrorAction
from src.market import MarketDataService
from src.indicators.manager import UnifiedIndicatorManager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/indicators", tags=["indicators"])


def _available_indicator_names(manager: UnifiedIndicatorManager) -> set[str]:
    return {str(info["name"]) for info in manager.list_indicators()}


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
    manager: UnifiedIndicatorManager = Depends(get_unified_indicator_manager)
) -> ApiResponse[List[str]]:
    """
    获取可用的指标列表
    
    Returns:
        可用的指标名称列表
    """
    try:
        # 从管理器获取指标列表
        indicators_info = manager.list_indicators()
        indicators = [info["name"] for info in indicators_info]
        
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
            error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
            error_message=f"获取指标列表失败: {str(e)}",
            suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
            details={"exception_type": type(e).__name__}
        )


@router.get("/performance/stats", response_model=ApiResponse[Dict[str, Any]])
async def get_performance_stats(
    manager: UnifiedIndicatorManager = Depends(get_unified_indicator_manager)
) -> ApiResponse[Dict[str, Any]]:
    """
    鑾峰彇浼樺寲鎸囨爣鏈嶅姟鐨勬€ц兘缁熻
    
    Returns:
        鎬ц兘缁熻淇℃伅
    """
    try:
        stats = manager.get_performance_stats()
        
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
            error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
            error_message=f"鑾峰彇鎬ц兘缁熻澶辫触: {str(e)}",
            suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
            details={"exception_type": type(e).__name__}
        )


@router.post("/cache/clear", response_model=ApiResponse[Dict[str, int]])
async def clear_cache(
    manager: UnifiedIndicatorManager = Depends(get_unified_indicator_manager)
) -> ApiResponse[Dict[str, int]]:
    """
    娓呯┖鎸囨爣缂撳瓨
    
    Returns:
        娓呴櫎鐨勭紦瀛橀」鏁伴噺
    """
    try:
        cache_count = manager.clear_cache()
        
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
            error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
            error_message=f"娓呯┖缂撳瓨澶辫触: {str(e)}",
            suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
            details={"exception_type": type(e).__name__}
        )


@router.get("/dependency/graph", response_model=ApiResponse[Dict[str, str]])
async def get_dependency_graph(
    format: str = Query("mermaid", description="鍥惧舰鏍煎紡: mermaid 鎴?dot"),
    manager: UnifiedIndicatorManager = Depends(get_unified_indicator_manager)
) -> ApiResponse[Dict[str, str]]:
    """
    鑾峰彇鎸囨爣渚濊禆鍏崇郴鍥?
    
    Args:
        format: 鍥惧舰鏍煎紡锛坢ermaid 鎴?dot锛?
        
    Returns:
        渚濊禆鍏崇郴鍥?
    """
    try:
        graph = manager.get_dependency_graph(format)
        
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
            error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
            error_message=f"鑾峰彇渚濊禆鍏崇郴鍥惧け璐? {str(e)}",
            suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
            details={"exception_type": type(e).__name__, "format": format}
        )


@router.get("/{symbol}/{timeframe}/live", response_model=ApiResponse[Dict[str, Any]])
async def get_intrabar_indicators(
    symbol: str,
    timeframe: str,
    manager: UnifiedIndicatorManager = Depends(get_unified_indicator_manager),
) -> ApiResponse[Dict[str, Any]]:
    """
    获取当前活跃K线（未收盘）的实时指标快照

    只返回策略声明需要的 intrabar 指标（启动时由 preferred_scopes +
    required_indicators 自动推导）。

    Returns:
        ``bar_time`` + ``indicators`` 字典，若尚无快照则返回 404。
    """
    try:
        snapshot = manager.get_intrabar_snapshot(symbol, timeframe)
        if snapshot is None:
            return ApiResponse.error_response(
                error_code=AIErrorCode.DATA_NOT_AVAILABLE,
                error_message=f"没有找到 {symbol}/{timeframe} 的实时指标快照（intrabar 未启用或尚未收到数据）",
                suggested_action=AIErrorAction.USE_FALLBACK_DATA,
                details={"symbol": symbol, "timeframe": timeframe},
            )
        bar_time, indicators = snapshot
        return ApiResponse.success_response(
            data={"bar_time": bar_time.isoformat(), "indicators": indicators},
            metadata={
                "symbol": symbol,
                "timeframe": timeframe,
                "count": len(indicators),
                "source": "intrabar_preview_snapshot",
            },
        )
    except Exception as e:
        logger.error("Failed to get intrabar indicators for %s/%s: %s", symbol, timeframe, e)
        return ApiResponse.error_response(
            error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
            error_message=f"获取实时指标数据失败: {str(e)}",
            suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
            details={"exception_type": type(e).__name__, "symbol": symbol, "timeframe": timeframe},
        )


@router.get("/{symbol}/{timeframe}", response_model=ApiResponse[Dict[str, Dict[str, Any]]])
async def get_indicators(
    symbol: str,
    timeframe: str,
    manager: UnifiedIndicatorManager = Depends(get_unified_indicator_manager)
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
        indicators = manager.get_all_indicators(symbol, timeframe)
        
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
            error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
            error_message=f"获取指标数据失败: {str(e)}",
            suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
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
    manager: UnifiedIndicatorManager = Depends(get_unified_indicator_manager)
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
        indicator_value = manager.get_indicator(symbol, timeframe, indicator_name)
        
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
            error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
            error_message=f"获取指标数据失败: {str(e)}",
            suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
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
    manager: UnifiedIndicatorManager = Depends(get_unified_indicator_manager),
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

        available_indicators = _available_indicator_names(manager)
        invalid_indicators = [
            indicator_name for indicator_name in request.indicators if indicator_name not in available_indicators
        ]
        if invalid_indicators:
            return ApiResponse.error_response(
                error_code=AIErrorCode.INVALID_INDICATOR_PARAMS,
                error_message=f"Unsupported indicators requested: {', '.join(invalid_indicators)}",
                suggested_action=AIErrorAction.VALIDATE_PARAMETERS,
                details={
                    "symbol": request.symbol,
                    "timeframe": request.timeframe,
                    "invalid_indicators": invalid_indicators,
                },
            )
        
        # 按需计算指标
        results = manager.compute(
            symbol=request.symbol,
            timeframe=request.timeframe,
            indicator_names=request.indicators
        )
        sanitized_results = {
            name: value
            for name, value in (results or {}).items()
            if isinstance(value, dict) and value
        }
        
        if not sanitized_results:
            return ApiResponse.error_response(
                error_code=AIErrorCode.DATA_NOT_AVAILABLE,
                error_message=f"无法计算 {request.symbol}/{request.timeframe} 的指标",
                suggested_action=AIErrorAction.USE_FALLBACK_DATA,
                details={
                    "symbol": request.symbol,
                    "timeframe": request.timeframe,
                    "indicators": request.indicators,
                    "empty_or_invalid_results": sorted((results or {}).keys()),
                }
            )
        
        return ApiResponse.success_response(
            data=sanitized_results,
            metadata={
                "symbol": request.symbol,
                "timeframe": request.timeframe,
                "indicators_requested": len(request.indicators),
                "indicators_computed": len(sanitized_results),
                "source": "optimized_indicator_service_on_demand"
            }
        )
    except Exception as e:
        logger.error(f"Failed to compute indicators: {e}")
        return ApiResponse.error_response(
            error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
            error_message=f"计算指标失败: {str(e)}",
            suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
            details={
                "exception_type": type(e).__name__,
                "symbol": request.symbol,
                "timeframe": request.timeframe,
                "indicators": request.indicators
            }
        )


