from __future__ import annotations

from fastapi import APIRouter

from .market_routes import query_router, stream_router
from .market_routes.query import (
    quote,
    quote_history,
    symbol_info,
    symbols,
    tick_history,
    ticks,
    ohlc,
    ohlc_history,
    ohlc_intrabar_series,
)
from .market_routes.stream import stream

router = APIRouter(tags=["market"])
router.include_router(query_router)
router.include_router(stream_router)

__all__ = [
    "ohlc",
    "ohlc_history",
    "ohlc_intrabar_series",
    "quote",
    "quote_history",
    "router",
    "stream",
    "symbol_info",
    "symbols",
    "tick_history",
    "ticks",
]
