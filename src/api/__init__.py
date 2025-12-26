"""
FastAPI 入口：组装各个路由模块，启动/关闭后台采集器。
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI

from src.api import account, market, trade
from src.api import deps
from src.api.schemas import ApiResponse
from src.clients.mt5_market import MT5MarketError
from src.core.market_service import MarketDataService
from src.core.trading_service import TradingService

logger = logging.getLogger(__name__)

app = FastAPI(title="MT5 Market Data Service", version="0.4.0", lifespan=deps.lifespan)


@app.get("/health", response_model=ApiResponse[dict])
def health(
    service: MarketDataService = Depends(deps.get_market_service),
    trading: TradingService = Depends(deps.get_trading_service),
) -> ApiResponse[dict]:
    """
    健康检查：返回行情、交易客户端状态以及采集队列长度。
    """
    try:
        market_status = service.health()
    except MT5MarketError as exc:
        market_status = {"connected": False, "error": str(exc)}
    trading_status = trading.client.health() if hasattr(trading, "client") else {}
    queues = deps.ingestor.queue_stats()
    return ApiResponse(
        success=True,
        data={"market": market_status, "trading": trading_status, "ingestor": {"queues": queues}},
    )


# 模块化路由
app.include_router(market.router)
app.include_router(account.router)
app.include_router(trade.router)


__all__ = ["app"]
