"""
简化版FastAPI应用 - 不依赖pydantic
用于在没有pydantic的情况下运行系统
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from src.api.deps_simple import lifespan
from src.api.market import router as market_router
from src.api.account import router as account_router
from src.api.trade import router as trade_router
from src.api.monitoring import router as monitoring_router

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# 创建FastAPI应用
app = FastAPI(
    title="MT5Services简化版",
    description="不依赖pydantic的MT5数据服务API",
    version="1.0.0-simple",
    lifespan=lifespan,
)

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(market_router, prefix="/api/market", tags=["market"])
app.include_router(account_router, prefix="/api/account", tags=["account"])
app.include_router(trade_router, prefix="/api/trade", tags=["trade"])
app.include_router(monitoring_router, prefix="/api/monitoring", tags=["monitoring"])

@app.get("/")
async def root():
    """根端点"""
    return {
        "service": "MT5Services简化版",
        "version": "1.0.0-simple",
        "status": "running",
        "description": "不依赖pydantic的MT5数据服务",
        "endpoints": {
            "market": "/api/market",
            "account": "/api/account",
            "trade": "/api/trade",
            "monitoring": "/api/monitoring",
        }
    }

@app.get("/health")
async def health():
    """健康检查端点"""
    return {
        "status": "healthy",
        "service": "MT5Services简化版",
        "pydantic_required": False,
    }

if __name__ == "__main__":
    import uvicorn
    logger.info("启动MT5Services简化版服务器...")
    uvicorn.run(
        "src.api.app_simple:app",
        host="0.0.0.0",
        port=8810,
        reload=False,  # 简化版不支持热重载
        log_level="info",
    )