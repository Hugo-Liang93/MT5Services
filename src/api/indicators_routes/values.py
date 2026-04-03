from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends

from src.api.deps import get_market_service, get_unified_indicator_manager
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse
from src.indicators.manager import UnifiedIndicatorManager
from src.market import MarketDataService

from .models import IndicatorRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/indicators", tags=["indicators"])


def _available_indicator_names(manager: UnifiedIndicatorManager) -> set[str]:
    return {str(info["name"]) for info in manager.list_indicators()}


@router.get("/{symbol}/{timeframe}/live", response_model=ApiResponse[Dict[str, Any]])
async def get_intrabar_indicators(symbol: str, timeframe: str, manager: UnifiedIndicatorManager = Depends(get_unified_indicator_manager)) -> ApiResponse[Dict[str, Any]]:
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
            metadata={"symbol": symbol, "timeframe": timeframe, "count": len(indicators), "source": "intrabar_preview_snapshot"},
        )
    except Exception as exc:
        logger.error("Failed to get intrabar indicators for %s/%s: %s", symbol, timeframe, exc)
        return ApiResponse.error_response(
            error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
            error_message=f"获取实时指标数据失败: {str(exc)}",
            suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
            details={"exception_type": type(exc).__name__, "symbol": symbol, "timeframe": timeframe},
        )


@router.get("/{symbol}/{timeframe}", response_model=ApiResponse[Dict[str, Dict[str, Any]]])
async def get_indicators(symbol: str, timeframe: str, manager: UnifiedIndicatorManager = Depends(get_unified_indicator_manager)) -> ApiResponse[Dict[str, Dict[str, Any]]]:
    try:
        indicators = manager.get_all_indicators(symbol, timeframe)
        if not indicators:
            return ApiResponse.error_response(
                error_code=AIErrorCode.DATA_NOT_AVAILABLE,
                error_message=f"没有找到 {symbol}/{timeframe} 的指标数据",
                suggested_action=AIErrorAction.USE_FALLBACK_DATA,
                details={"symbol": symbol, "timeframe": timeframe},
            )
        return ApiResponse.success_response(
            data=indicators,
            metadata={"symbol": symbol, "timeframe": timeframe, "count": len(indicators), "source": "optimized_indicator_service"},
        )
    except Exception as exc:
        logger.error("Failed to get indicators for %s/%s: %s", symbol, timeframe, exc)
        return ApiResponse.error_response(
            error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
            error_message=f"获取指标数据失败: {str(exc)}",
            suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
            details={"exception_type": type(exc).__name__, "symbol": symbol, "timeframe": timeframe},
        )


@router.get("/{symbol}/{timeframe}/{indicator_name}", response_model=ApiResponse[Dict[str, Any]])
async def get_indicator(symbol: str, timeframe: str, indicator_name: str, manager: UnifiedIndicatorManager = Depends(get_unified_indicator_manager)) -> ApiResponse[Dict[str, Any]]:
    try:
        indicator_value = manager.get_indicator(symbol, timeframe, indicator_name)
        if indicator_value is None:
            return ApiResponse.error_response(
                error_code=AIErrorCode.DATA_NOT_AVAILABLE,
                error_message=f"没有找到 {symbol}/{timeframe}/{indicator_name} 的指标数据",
                suggested_action=AIErrorAction.USE_FALLBACK_DATA,
                details={"symbol": symbol, "timeframe": timeframe, "indicator_name": indicator_name},
            )
        return ApiResponse.success_response(
            data=indicator_value,
            metadata={"symbol": symbol, "timeframe": timeframe, "indicator_name": indicator_name, "source": "optimized_indicator_service"},
        )
    except Exception as exc:
        logger.error("Failed to get indicator %s for %s/%s: %s", indicator_name, symbol, timeframe, exc)
        return ApiResponse.error_response(
            error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
            error_message=f"获取指标数据失败: {str(exc)}",
            suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
            details={"exception_type": type(exc).__name__, "symbol": symbol, "timeframe": timeframe, "indicator_name": indicator_name},
        )


@router.post("/compute", response_model=ApiResponse[Dict[str, Dict[str, Any]]])
async def compute_indicators(request: IndicatorRequest, manager: UnifiedIndicatorManager = Depends(get_unified_indicator_manager), market_service: MarketDataService = Depends(get_market_service)) -> ApiResponse[Dict[str, Dict[str, Any]]]:
    try:
        symbols = market_service.list_symbols()
        if request.symbol not in symbols:
            return ApiResponse.error_response(
                error_code=AIErrorCode.MT5_SYMBOL_NOT_FOUND,
                error_message=f"交易品种 '{request.symbol}' 不存在",
                suggested_action=AIErrorAction.USE_DIFFERENT_SYMBOL,
                details={"symbol": request.symbol, "available_symbols": symbols},
            )
        available = _available_indicator_names(manager)
        invalid = [name for name in request.indicators if name not in available]
        if invalid:
            return ApiResponse.error_response(
                error_code=AIErrorCode.INVALID_INDICATOR_PARAMS,
                error_message=f"Unsupported indicators requested: {', '.join(invalid)}",
                suggested_action=AIErrorAction.VALIDATE_PARAMETERS,
                details={"symbol": request.symbol, "timeframe": request.timeframe, "invalid_indicators": invalid},
            )
        results = manager.compute(symbol=request.symbol, timeframe=request.timeframe, indicator_names=request.indicators)
        sanitized = {name: value for name, value in (results or {}).items() if isinstance(value, dict) and value}
        if not sanitized:
            return ApiResponse.error_response(
                error_code=AIErrorCode.DATA_NOT_AVAILABLE,
                error_message=f"无法计算 {request.symbol}/{request.timeframe} 的指标",
                suggested_action=AIErrorAction.USE_FALLBACK_DATA,
                details={"symbol": request.symbol, "timeframe": request.timeframe, "indicators": request.indicators, "empty_or_invalid_results": sorted((results or {}).keys())},
            )
        return ApiResponse.success_response(
            data=sanitized,
            metadata={"symbol": request.symbol, "timeframe": request.timeframe, "indicators_requested": len(request.indicators), "indicators_computed": len(sanitized), "source": "optimized_indicator_service_on_demand"},
        )
    except Exception as exc:
        logger.error("Failed to compute indicators: %s", exc)
        return ApiResponse.error_response(
            error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
            error_message=f"计算指标失败: {str(exc)}",
            suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
            details={"exception_type": type(exc).__name__, "symbol": request.symbol, "timeframe": request.timeframe, "indicators": request.indicators},
        )
