"""
增强的FastAPI入口：使用优化后的组件和监控系统
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api import account, market, trade, monitoring
from src.api import deps_enhanced
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse
from src.clients.mt5_market import MT5MarketError
from src.core.market_service import MarketDataService
from src.core.trading_service import TradingService

logger = logging.getLogger(__name__)

# 创建FastAPI应用，使用增强的生命周期管理
app = FastAPI(
    title="MT5 Market Data Service (Enhanced)",
    version="1.0.0",
    description="增强版的MT5市场数据服务，包含优化的事件存储、缓存一致性和监控系统",
    lifespan=deps_enhanced.lifespan_enhanced
)

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应该限制来源
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", summary="API根目录")
async def root():
    """
    API根目录，返回服务信息
    """
    return {
        "service": "MT5 Market Data Service (Enhanced)",
        "version": "1.0.0",
        "description": "增强版的MT5市场数据服务",
        "endpoints": {
            "health": "/health",
            "market_symbols": "/symbols",
            "market_quote": "/quote",
            "account_info": "/account/info",
            "trade_open": "/trade",
            "monitoring_health": "/monitoring/health"
        }
    }


@app.get("/health", response_model=ApiResponse[dict], summary="基础健康检查")
def health(
    service: MarketDataService = Depends(deps_enhanced.get_market_service),
    trading: TradingService = Depends(deps_enhanced.get_trading_service),
) -> ApiResponse[dict]:
    """
    基础健康检查：返回行情、交易客户端状态以及采集队列长度。
    """
    try:
        market_status = service.health()
    except MT5MarketError as exc:
        market_status = {"connected": False, "error": str(exc)}
    
    trading_status = trading.client.health() if hasattr(trading, "client") else {}
    
    # 获取队列状态
    ingestor = deps_enhanced.get_ingestor()
    queues = ingestor.queue_stats()
    
    # 获取监控状态
    health_monitor = deps_enhanced.get_health_monitor_instance()
    system_status = health_monitor.get_system_status()
    
    return ApiResponse(
        success=True,
        data={
            "market": market_status, 
            "trading": trading_status, 
            "ingestor": {"queues": queues},
            "monitoring": {
                "overall_status": system_status.get("overall_status", "unknown"),
                "active_alerts": len(system_status.get("active_alerts", []))
            }
        },
    )


# 模块化路由
app.include_router(market.router)
app.include_router(account.router)
app.include_router(trade.router)
app.include_router(monitoring.router)  # 添加监控路由


# 自定义异常处理
@app.exception_handler(MT5MarketError)
async def mt5_market_error_handler(request, exc):
    """
    MT5市场错误处理
    """
    logger.error(f"MT5MarketError: {exc}")
    payload = ApiResponse.error_response(
        error_code=AIErrorCode.MT5_CONNECTION_FAILED,
        error_message=str(exc),
        suggested_action=AIErrorAction.CHECK_CONNECTION,
        details={"exception_type": type(exc).__name__},
    )
    return JSONResponse(status_code=503, content=payload.dict())


@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    """
    通用异常处理
    """
    logger.exception(f"Unhandled exception: {exc}")
    payload = ApiResponse.error_response(
        error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
        error_message="Internal server error",
        suggested_action=AIErrorAction.CONTACT_SUPPORT,
        details={"exception_type": type(exc).__name__},
    )
    return JSONResponse(status_code=500, content=payload.dict())


__all__ = ["app"]
