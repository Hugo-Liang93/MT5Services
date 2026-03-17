"""
Unified FastAPI app entrypoint.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api import account, economic, indicators, market, monitoring, signal, trade
from src.api import deps
from src.api.schemas import ApiResponse
from src.clients.mt5_market import MT5MarketError
from src.config import get_api_config
from src.core.market_service import MarketDataService
from src.trading.service import TradingModule

logger = logging.getLogger(__name__)
api_config = get_api_config()
AUTH_EXEMPT_PATHS = {
    "/health",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}


def _resolve_expected_api_key() -> str | None:
    configured_key = (api_config.api_key or "").strip() if api_config.api_key else ""
    return configured_key or None

app = FastAPI(
    title="MT5 Market Data Service",
    version="1.0.0",
    lifespan=deps.lifespan,
    docs_url="/docs" if api_config.docs_enabled else None,
    redoc_url="/redoc" if api_config.redoc_enabled else None,
)

if api_config.enable_cors:
    cors_allow_origins = ["*"]
    # Browsers do not allow credentialed requests when origin is wildcard.
    cors_allow_credentials = "*" not in cors_allow_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_allow_origins,
        allow_credentials=cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def api_key_authentication(request: Request, call_next):
    if not api_config.auth_enabled:
        return await call_next(request)

    if request.url.path in AUTH_EXEMPT_PATHS:
        return await call_next(request)

    expected_api_key = _resolve_expected_api_key()
    if not expected_api_key:
        logger.error("API authentication is enabled but no API key is configured")
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "error": {
                    "code": "api_auth_not_configured",
                    "message": "API authentication is enabled but no API key is configured",
                },
            },
        )

    provided_api_key = request.headers.get(api_config.api_key_header, "").strip()
    if not provided_api_key or not hmac.compare_digest(provided_api_key, expected_api_key):
        return JSONResponse(
            status_code=401,
            content={
                "success": False,
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid or missing API key",
                },
            },
            headers={"WWW-Authenticate": "API-Key"},
        )

    return await call_next(request)


@app.get("/health", response_model=ApiResponse[dict])
def health(
    service: MarketDataService = Depends(deps.get_market_service),
    trading: TradingModule = Depends(deps.get_trading_service),
) -> ApiResponse[dict]:
    try:
        market_status = service.health()
    except MT5MarketError as exc:
        market_status = {"connected": False, "error": str(exc)}
    trading_status = trading.health()
    queues = deps.get_ingestor().queue_stats()

    return ApiResponse(
        success=True,
        data={
            "mode": deps.get_runtime_mode(),
            "market": market_status,
            "trading": trading_status,
            "ingestor": {"queues": queues},
        },
    )


app.include_router(market.router)
app.include_router(economic.router)
app.include_router(account.router)
app.include_router(trade.router)
app.include_router(monitoring.router)
app.include_router(indicators.router)
app.include_router(signal.router)


__all__ = ["app"]
