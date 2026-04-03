from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from src.api.deps import get_market_service
from src.market import MarketDataService

from .common import runtime_market_defaults

router = APIRouter(tags=["market"])


@router.get("/stream")
async def stream(
    symbol: Optional[str] = Query(default=None, description="交易品种，可为空则使用默认"),
    interval: Optional[float] = Query(default=None, gt=0.0, description="SSE 推送间隔（秒）"),
    service: MarketDataService = Depends(get_market_service),
):
    defaults = runtime_market_defaults()
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
            except Exception as exc:
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            await asyncio.sleep(poll)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
