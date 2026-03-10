"""
绠€鍖栫増FastAPI搴旂敤 - 涓嶄緷璧杙ydantic
鐢ㄤ簬鍦ㄦ病鏈塸ydantic鐨勬儏鍐典笅杩愯绯荤粺
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from src.api import deps as shared_deps
from src.api.deps_simple import (
    lifespan,
    get_market_service as get_market_service_simple,
    get_account_service as get_account_service_simple,
    get_trading_service as get_trading_service_simple,
)
from src.api.market import router as market_router
from src.api.account import router as account_router
from src.api.trade import router as trade_router

# 閰嶇疆鏃ュ織
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# 鍒涘缓FastAPI搴旂敤
app = FastAPI(
    title="MT5Services绠€鍖栫増",
    description="涓嶄緷璧杙ydantic鐨凪T5鏁版嵁鏈嶅姟API",
    version="1.0.0-simple",
    lifespan=lifespan,
)

# 娣诲姞CORS涓棿浠?
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 娉ㄥ唽璺敱
app.include_router(market_router, prefix="/api/market", tags=["market"])
app.include_router(account_router, prefix="/api", tags=["account"])
app.include_router(trade_router, prefix="/api", tags=["trade"])

# Let shared routers use simple-mode dependencies.
app.dependency_overrides[shared_deps.get_market_service] = get_market_service_simple
app.dependency_overrides[shared_deps.get_account_service] = get_account_service_simple
app.dependency_overrides[shared_deps.get_trading_service] = get_trading_service_simple

@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": "MT5Services绠€鍖栫増",
        "version": "1.0.0-simple",
        "status": "running",
        "description": "涓嶄緷璧杙ydantic鐨凪T5鏁版嵁鏈嶅姟",
        "endpoints": {
            "market": "/api/market",
            "account": "/api/account",
            "trade": "/api/trade",
        }
    }

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "MT5Services绠€鍖栫増",
        "pydantic_required": False,
    }

if __name__ == "__main__":
    import uvicorn
    logger.info("鍚姩MT5Services绠€鍖栫増鏈嶅姟鍣?..")
    uvicorn.run(
        "src.api.app_simple:app",
        host="0.0.0.0",
        port=8810,
        reload=False,  # 绠€鍖栫増涓嶆敮鎸佺儹閲嶈浇
        log_level="info",
    )

