from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from src.api.deps import get_market_service
from src.api.schemas import ApiResponse, OHLCModel, QuoteModel, TickModel, SymbolInfoModel
from src.api.error_codes import AIErrorCode, AIErrorAction, get_suggested_action
from src.clients.mt5_market import MT5MarketError
from src.config import get_interval_config, get_limit_config, get_shared_default_symbol
from src.market import MarketDataService

router = APIRouter(tags=["market"])


def _runtime_market_defaults() -> dict:
    limits = get_limit_config()
    intervals = get_interval_config()
    return {
        "default_symbol": get_shared_default_symbol(),
        "tick_limit": limits.tick_limit,
        "ohlc_limit": limits.ohlc_limit,
        "stream_interval_seconds": intervals.stream_interval,
    }


def _normalize_query_time(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _model_payload(item) -> dict:
    payload = dict(getattr(item, "__dict__", {}) or {})
    if getattr(item, "time", None) is not None:
        payload["time"] = item.time.isoformat()
    return payload


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
    defaults = _runtime_market_defaults()
    resolved_symbol = symbol or defaults["default_symbol"]
    try:
        data = service.get_quote(resolved_symbol)
        if data is None:
            return ApiResponse.error_response(
                error_code=AIErrorCode.DATA_NOT_AVAILABLE,
                error_message="报价数据暂时不可用",
                suggested_action=AIErrorAction.USE_FALLBACK_DATA,
                details={"symbol": resolved_symbol}
            )
        
        return ApiResponse.success_response(
            data=QuoteModel(**_model_payload(data)),
            metadata={
                "symbol": resolved_symbol,
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
            details={"exception_type": type(exc).__name__, "symbol": resolved_symbol}
        )


@router.get("/ticks", response_model=ApiResponse[List[TickModel]])
def ticks(
    symbol: Optional[str] = Query(default=None, description="交易品种，可为空则使用默认"),
    limit: Optional[int] = Query(default=None, ge=1, le=10000),
    service: MarketDataService = Depends(get_market_service),
) -> ApiResponse[List[TickModel]]:
    defaults = _runtime_market_defaults()
    resolved_symbol = symbol or defaults["default_symbol"]
    try:
        data = service.get_ticks(resolved_symbol, limit)
        if not data:
            return ApiResponse.error_response(
                error_code=AIErrorCode.DATA_NOT_AVAILABLE,
                error_message="Tick数据暂时不可用",
                suggested_action=AIErrorAction.WAIT_FOR_DATA,
                details={"symbol": resolved_symbol, "limit": limit}
            )
        
        items = [TickModel(**_model_payload(t)) for t in data]
        return ApiResponse.success_response(
            data=items,
            metadata={
                "symbol": resolved_symbol,
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
            details={"exception_type": type(exc).__name__, "symbol": resolved_symbol}
        )


@router.get("/quotes/history", response_model=ApiResponse[List[QuoteModel]])
def quote_history(
    symbol: Optional[str] = Query(default=None, description="symbol"),
    start_time: Optional[datetime] = Query(default=None, description="inclusive start time"),
    end_time: Optional[datetime] = Query(default=None, description="inclusive end time"),
    limit: int = Query(default=1000, ge=1, le=50000),
    strict: bool = Query(default=False, description="require persisted history"),
    service: MarketDataService = Depends(get_market_service),
) -> ApiResponse[List[QuoteModel]]:
    resolved_symbol = symbol or _runtime_market_defaults()["default_symbol"]
    start_time = _normalize_query_time(start_time)
    end_time = _normalize_query_time(end_time)
    try:
        data, source = service.get_quote_history_result(
            resolved_symbol,
            start_time,
            end_time,
            limit,
            strict=strict,
        )
    except RuntimeError as exc:
        return ApiResponse.error_response(
            error=str(exc),
            error_code=AIErrorCode.SERVICE_UNAVAILABLE,
        )
    items = [QuoteModel(**_model_payload(quote)) for quote in data]
    return ApiResponse.success_response(
        data=items,
        metadata={
            "symbol": resolved_symbol,
            "data_type": "quote_history",
            "count": len(items),
            "time_range": {
                "start": start_time.isoformat() if start_time else None,
                "end": end_time.isoformat() if end_time else None,
                "first": items[0].time if items else None,
                "last": items[-1].time if items else None,
            },
            "source": source,
            "data_source": source,
            "data_freshness": "historical",
            "strict": strict,
        },
    )


@router.get("/ticks/history", response_model=ApiResponse[List[TickModel]])
def tick_history(
    symbol: Optional[str] = Query(default=None, description="symbol"),
    start_time: Optional[datetime] = Query(default=None, description="inclusive start time"),
    end_time: Optional[datetime] = Query(default=None, description="inclusive end time"),
    limit: int = Query(default=5000, ge=1, le=100000),
    strict: bool = Query(default=False, description="require persisted history"),
    service: MarketDataService = Depends(get_market_service),
) -> ApiResponse[List[TickModel]]:
    resolved_symbol = symbol or _runtime_market_defaults()["default_symbol"]
    start_time = _normalize_query_time(start_time)
    end_time = _normalize_query_time(end_time)
    try:
        data, source = service.get_ticks_history_result(
            resolved_symbol,
            start_time,
            end_time,
            limit,
            strict=strict,
        )
    except RuntimeError as exc:
        return ApiResponse.error_response(
            error=str(exc),
            error_code=AIErrorCode.SERVICE_UNAVAILABLE,
        )
    items = [TickModel(**_model_payload(tick)) for tick in data]
    return ApiResponse.success_response(
        data=items,
        metadata={
            "symbol": resolved_symbol,
            "data_type": "tick_history",
            "count": len(items),
            "time_range": {
                "start": start_time.isoformat() if start_time else None,
                "end": end_time.isoformat() if end_time else None,
                "first": items[0].time if items else None,
                "last": items[-1].time if items else None,
            },
            "source": source,
            "data_source": source,
            "data_freshness": "historical",
            "strict": strict,
        },
    )


@router.get("/ohlc", response_model=ApiResponse[List[OHLCModel]])
def ohlc(
    symbol: Optional[str] = Query(default=None, description="交易品种，可为空则使用默认"),
    timeframe: str = Query(default="M1", description="MT5 时间框架，可选 M1/M5/H1/D1"),
    limit: Optional[int] = Query(default=None, ge=1, le=5000),
    service: MarketDataService = Depends(get_market_service),
) -> ApiResponse[List[OHLCModel]]:
    defaults = _runtime_market_defaults()
    resolved_symbol = symbol or defaults["default_symbol"]
    resolved_limit = limit or defaults["ohlc_limit"]
    try:
        data = service.get_ohlc_closed(resolved_symbol, timeframe, resolved_limit)
        if not data:
            return ApiResponse.error_response(
                error_code=AIErrorCode.DATA_NOT_AVAILABLE,
                error_message="OHLC数据暂时不可用",
                suggested_action=AIErrorAction.WAIT_FOR_DATA,
                details={"symbol": resolved_symbol, "timeframe": timeframe, "limit": resolved_limit}
            )
        
        items = [OHLCModel(**_model_payload(bar)) for bar in data]
        return ApiResponse.success_response(
            data=items,
            metadata={
                "symbol": resolved_symbol,
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
            details={"exception_type": type(exc).__name__, "symbol": resolved_symbol, "timeframe": timeframe}
        )


@router.get("/ohlc/history", response_model=ApiResponse[List[OHLCModel]])
def ohlc_history(
    symbol: Optional[str] = Query(default=None, description="symbol"),
    timeframe: str = Query(default="M1", description="timeframe"),
    start_time: Optional[datetime] = Query(default=None, description="inclusive start time"),
    end_time: Optional[datetime] = Query(default=None, description="inclusive end time"),
    limit: int = Query(default=5000, ge=1, le=50000),
    strict: bool = Query(default=False, description="require persisted history"),
    service: MarketDataService = Depends(get_market_service),
) -> ApiResponse[List[OHLCModel]]:
    resolved_symbol = symbol or _runtime_market_defaults()["default_symbol"]
    start_time = _normalize_query_time(start_time)
    end_time = _normalize_query_time(end_time)
    try:
        data, source = service.get_ohlc_history_result(
            resolved_symbol,
            timeframe,
            start_time,
            end_time,
            limit,
            strict=strict,
        )
    except RuntimeError as exc:
        return ApiResponse.error_response(
            error=str(exc),
            error_code=AIErrorCode.SERVICE_UNAVAILABLE,
        )
    items = [OHLCModel(**_model_payload(bar)) for bar in data]
    return ApiResponse.success_response(
        data=items,
        metadata={
            "symbol": resolved_symbol,
            "timeframe": timeframe,
            "data_type": "ohlc_history",
            "count": len(items),
            "time_range": {
                "start": start_time.isoformat() if start_time else None,
                "end": end_time.isoformat() if end_time else None,
                "first": items[0].time if items else None,
                "last": items[-1].time if items else None,
            },
            "source": source,
            "data_source": source,
            "data_freshness": "historical",
            "strict": strict,
            "includes_indicators": any(item.indicators for item in items),
        },
    )


@router.get("/ohlc/intrabar/series", response_model=ApiResponse[List[OHLCModel]])
def ohlc_intrabar_series(
    symbol: Optional[str] = Query(default=None, description="交易品种，可为空则使用默认"),
    timeframe: str = Query(default="M1", description="MT5 时间框架，可选 M1/M5/H1/D1"),
    service: MarketDataService = Depends(get_market_service),
) -> ApiResponse[List[OHLCModel]]:
    resolved_symbol = symbol or _runtime_market_defaults()["default_symbol"]
    data = service.get_intrabar_series(resolved_symbol, timeframe)
    items = [OHLCModel(**_model_payload(bar)) for bar in data]
    return ApiResponse.success_response(
        data=items,
        metadata={
            "symbol": resolved_symbol,
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
    defaults = _runtime_market_defaults()
    resolved_symbol = symbol or defaults["default_symbol"]
    poll = interval or defaults["stream_interval_seconds"]

    async def event_generator():
        while True:
            try:
                data = service.get_quote(resolved_symbol)
                if data is None:
                    yield f"data: {json.dumps({'error': 'quote cache empty'})}\n\n"
                else:
                    yield f"data: {json.dumps({'bid': data.bid, 'ask': data.ask, 'time': data.time.isoformat()})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            await asyncio.sleep(poll)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
