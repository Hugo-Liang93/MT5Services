from __future__ import annotations

import asyncio
import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from src.api.deps import get_market_service
from src.api.schemas import ApiResponse, OHLCModel, QuoteModel, TickModel, SymbolInfoModel
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
        return ApiResponse(
            success=True,
            data=SymbolInfoModel(**info.__dict__),
        )
    except MT5MarketError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/quote", response_model=ApiResponse[QuoteModel])
def quote(
    symbol: Optional[str] = Query(default=None, description="交易品种，可为空则使用默认"),
    service: MarketDataService = Depends(get_market_service),
) -> ApiResponse[QuoteModel]:
    settings = load_market_settings()
    try:
        data = service.get_quote(symbol or settings.default_symbol)
        if data is None:
            raise HTTPException(status_code=503, detail="Quote cache empty")
        return ApiResponse(success=True, data=QuoteModel(**data.__dict__, time=data.time.isoformat()))
    except MT5MarketError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/ticks", response_model=ApiResponse[List[TickModel]])
def ticks(
    symbol: Optional[str] = Query(default=None, description="交易品种，可为空则使用默认"),
    limit: Optional[int] = Query(default=None, ge=1, le=10000),
    service: MarketDataService = Depends(get_market_service),
) -> ApiResponse[List[TickModel]]:
    try:
        data = service.get_ticks(symbol, limit)
        items = [TickModel(**t.__dict__, time=t.time.isoformat()) for t in data]
        return ApiResponse(success=True, data=items)
    except MT5MarketError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/ohlc", response_model=ApiResponse[List[OHLCModel]])
def ohlc(
    symbol: Optional[str] = Query(default=None, description="交易品种，可为空则使用默认"),
    timeframe: str = Query(default="M1", description="MT5 时间框架，如 M1/M5/H1/D1"),
    limit: Optional[int] = Query(default=None, ge=1, le=5000),
    service: MarketDataService = Depends(get_market_service),
) -> ApiResponse[List[OHLCModel]]:
    try:
        data = service.get_ohlc_closed(symbol, timeframe, limit)
        items = [OHLCModel(**bar.__dict__, time=bar.time.isoformat()) for bar in data]
        return ApiResponse(success=True, data=items)
    except MT5MarketError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/ohlc/intrabar/series", response_model=ApiResponse[List[OHLCModel]])
def ohlc_intrabar_series(
    symbol: Optional[str] = Query(default=None, description="交易品种，可为空则使用默认"),
    timeframe: str = Query(default="M1", description="MT5 时间框架，如 M1/M5/H1/D1"),
    service: MarketDataService = Depends(get_market_service),
) -> ApiResponse[List[OHLCModel]]:
    data = service.get_intrabar_series(symbol, timeframe)
    items = [OHLCModel(**bar.__dict__, time=bar.time.isoformat()) for bar in data]
    return ApiResponse(success=True, data=items)


@router.get("/stream")
async def stream(
    symbol: Optional[str] = Query(default=None, description="交易品种，可为空则使用默认"),
    interval: Optional[float] = Query(default=None, gt=0.0, description="SSE 轮询间隔（秒）"),
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
                    payload = QuoteModel(**data.__dict__, time=data.time.isoformat()).model_dump()
                    yield f"data: {json.dumps(payload)}\n\n"
            except MT5MarketError as exc:
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            await asyncio.sleep(poll)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
