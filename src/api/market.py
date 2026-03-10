from __future__ import annotations

import asyncio
import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from src.api.deps import get_market_service
from src.api.schemas import ApiResponse, OHLCModel, QuoteModel, TickModel, SymbolInfoModel
from src.api.error_codes import AIErrorCode, AIErrorAction, get_suggested_action
from src.clients.mt5_market import MT5MarketError
from src.config import load_ingest_settings, load_market_settings
from src.core.market_service import MarketDataService

router = APIRouter(tags=["market"])


@router.get("/symbols")
def symbols(service: MarketDataService = Depends(get_market_service)) -> List[str]:
    return service.list_symbols()


@router.get("/symbol/info", response_model=ApiResponse[SymbolInfoModel])
def symbol_info(
    symbol: str = Query(..., description="交易品种"),
    service: MarketDataService = Depends(get_market_service),
) -> ApiResponse[SymbolInfoModel]:
    try:
        info = service.get_symbol_info(symbol)
        if info is None:
            return ApiResponse.error_response(
                error_code=AIErrorCode.MT5_SYMBOL_NOT_FOUND,
                error_message=f"交易品种 '{symbol}' 不存在",
                suggested_action=AIErrorAction.USE_DIFFERENT_SYMBOL,
                details={"symbol": symbol}
            )
        
        return ApiResponse.success_response(
            data=SymbolInfoModel(**info.__dict__),
            metadata={
                "symbol": symbol,
                "info_type": "symbol_info"
            }
        )
    except MT5MarketError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.MT5_CONNECTION_FAILED,
            error_message=f"MT5连接错误: {str(exc)}",
            suggested_action=AIErrorAction.CHECK_CONNECTION,
            details={"exception_type": type(exc).__name__, "symbol": symbol}
        )


@router.get("/quote", response_model=ApiResponse[QuoteModel])
def quote(
    symbol: Optional[str] = Query(default=None, description="交易品种，可为空则使用默认"),
    service: MarketDataService = Depends(get_market_service),
) -> ApiResponse[QuoteModel]:
    settings = load_market_settings()
    try:
        data = service.get_quote(symbol or settings.default_symbol)
        if data is None:
            return ApiResponse.error_response(
                error_code=AIErrorCode.DATA_NOT_AVAILABLE,
                error_message="报价数据暂时不可用",
                suggested_action=AIErrorAction.USE_FALLBACK_DATA,
                details={"symbol": symbol or settings.default_symbol}
            )
        
        return ApiResponse.success_response(
            data=QuoteModel(**data.__dict__, time=data.time.isoformat()),
            metadata={
                "symbol": symbol or settings.default_symbol,
                "data_type": "quote",
                "bid": data.bid,
                "ask": data.ask,
                "spread": data.ask - data.bid
            }
        )
    except MT5MarketError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.MT5_CONNECTION_FAILED,
            error_message=f"MT5连接错误: {str(exc)}",
            suggested_action=AIErrorAction.CHECK_CONNECTION,
            details={"exception_type": type(exc).__name__, "symbol": symbol or settings.default_symbol}
        )


@router.get("/ticks", response_model=ApiResponse[List[TickModel]])
def ticks(
    symbol: Optional[str] = Query(default=None, description="交易品种，可为空则使用默认"),
    limit: Optional[int] = Query(default=None, ge=1, le=10000),
    service: MarketDataService = Depends(get_market_service),
) -> ApiResponse[List[TickModel]]:
    settings = load_market_settings()
    try:
        data = service.get_ticks(symbol or settings.default_symbol, limit)
        if not data:
            return ApiResponse.error_response(
                error_code=AIErrorCode.DATA_NOT_AVAILABLE,
                error_message="Tick数据暂时不可用",
                suggested_action=AIErrorAction.WAIT_FOR_DATA,
                details={"symbol": symbol or settings.default_symbol, "limit": limit}
            )
        
        items = [TickModel(**t.__dict__, time=t.time.isoformat()) for t in data]
        return ApiResponse.success_response(
            data=items,
            metadata={
                "symbol": symbol or settings.default_symbol,
                "data_type": "ticks",
                "count": len(items),
                "time_range": {
                    "first": items[0].time if items else None,
                    "last": items[-1].time if items else None
                }
            }
        )
    except MT5MarketError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.MT5_CONNECTION_FAILED,
            error_message=f"MT5连接错误: {str(exc)}",
            suggested_action=AIErrorAction.CHECK_CONNECTION,
            details={"exception_type": type(exc).__name__, "symbol": symbol or settings.default_symbol}
        )


@router.get("/ohlc", response_model=ApiResponse[List[OHLCModel]])
def ohlc(
    symbol: Optional[str] = Query(default=None, description="交易品种，可为空则使用默认"),
    timeframe: str = Query(default="M1", description="MT5 时间框架，可选 M1/M5/H1/D1"),
    limit: Optional[int] = Query(default=None, ge=1, le=5000),
    service: MarketDataService = Depends(get_market_service),
) -> ApiResponse[List[OHLCModel]]:
    try:
        data = service.get_ohlc_closed(symbol, timeframe, limit)
        if not data:
            return ApiResponse.error_response(
                error_code=AIErrorCode.DATA_NOT_AVAILABLE,
                error_message="OHLC数据暂时不可用",
                suggested_action=AIErrorAction.WAIT_FOR_DATA,
                details={"symbol": symbol, "timeframe": timeframe, "limit": limit}
            )
        
        items = [OHLCModel(**bar.__dict__, time=bar.time.isoformat()) for bar in data]
        return ApiResponse.success_response(
            data=items,
            metadata={
                "symbol": symbol,
                "timeframe": timeframe,
                "data_type": "ohlc_closed",
                "count": len(items),
                "time_range": {
                    "first": items[0].time if items else None,
                    "last": items[-1].time if items else None
                }
            }
        )
    except MT5MarketError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.MT5_CONNECTION_FAILED,
            error_message=f"MT5连接错误: {str(exc)}",
            suggested_action=AIErrorAction.CHECK_CONNECTION,
            details={"exception_type": type(exc).__name__, "symbol": symbol, "timeframe": timeframe}
        )


@router.get("/ohlc/intrabar/series", response_model=ApiResponse[List[OHLCModel]])
def ohlc_intrabar_series(
    symbol: Optional[str] = Query(default=None, description="交易品种，可为空则使用默认"),
    timeframe: str = Query(default="M1", description="MT5 时间框架，可选 M1/M5/H1/D1"),
    service: MarketDataService = Depends(get_market_service),
) -> ApiResponse[List[OHLCModel]]:
    data = service.get_intrabar_series(symbol, timeframe)
    items = [OHLCModel(**bar.__dict__, time=bar.time.isoformat()) for bar in data]
    return ApiResponse.success_response(
        data=items,
        metadata={
            "symbol": symbol,
            "timeframe": timeframe,
            "data_type": "ohlc_intrabar",
            "count": len(items)
        }
    )


@router.get("/stream")
async def stream(
    symbol: Optional[str] = Query(default=None, description="交易品种，可为空则使用默认"),
    interval: Optional[float] = Query(default=None, gt=0.0, description="SSE 推送间隔（秒）"),
    service: MarketDataService = Depends(get_market_service),
):
    settings = load_market_settings()
    poll = interval or settings.stream_interval_seconds

    async def event_generator():
        while True:
            try:
                data = service.get_quote(symbol)
                if data is None:
                    yield f"data: {json.dumps({'error': 'quote cache empty'})}\n\n"
                else:
                    yield f"data: {json.dumps({'bid': data.bid, 'ask': data.ask, 'time': data.time.isoformat()})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            await asyncio.sleep(poll)

    return StreamingResponse(event_generator(), media_type="text/event-stream")