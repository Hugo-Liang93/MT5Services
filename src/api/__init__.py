"""Unified FastAPI application entrypoint."""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api import (
    account,
    admin,
    backtest,
    decision,
    deps,
    economic,
    experiment,
    indicators,
    market,
    monitoring,
    paper_trading,
    research,
    signal,
    studio,
    trade,
)
from src.api.schemas import ApiResponse
from src.clients.mt5_market import MT5MarketError
from src.config import get_api_config
from src.market import MarketDataService

logger = logging.getLogger(__name__)
api_config = get_api_config()

AUTH_EXEMPT_PATHS = {
    "/health",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}
AUTH_QUERY_PARAM_PATHS = {"/v1/studio/stream"}


def _resolve_expected_api_key() -> str | None:
    current_api_config = get_api_config()
    configured_key = (
        (current_api_config.api_key or "").strip() if current_api_config.api_key else ""
    )
    return configured_key or None


def _resolve_provided_api_key(request: Request, header_name: str) -> str:
    provided_api_key = request.headers.get(header_name, "").strip()
    if provided_api_key:
        return provided_api_key
    if request.url.path in AUTH_QUERY_PARAM_PATHS:
        return request.query_params.get("api_key", "").strip()
    return ""


app = FastAPI(
    title="MT5 Market Data Service",
    version="1.0.0",
    lifespan=deps.lifespan,
    docs_url="/docs" if api_config.docs_enabled else None,
    redoc_url="/redoc" if api_config.redoc_enabled else None,
)

if api_config.enable_cors:
    cors_allow_origins = ["*"]
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
    current_api_config = get_api_config()
    if not current_api_config.auth_enabled:
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
                    "message": (
                        "API authentication is enabled but no API key is configured"
                    ),
                },
            },
        )

    provided_api_key = _resolve_provided_api_key(
        request,
        current_api_config.api_key_header,
    )
    if not provided_api_key or not hmac.compare_digest(
        provided_api_key, expected_api_key
    ):
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
    trading=Depends(deps.get_trading_query_service),
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


v1 = APIRouter(prefix="/v1")
v1.include_router(market.router)
v1.include_router(decision.router)
v1.include_router(economic.router)
v1.include_router(account.router)
v1.include_router(trade.router)
v1.include_router(monitoring.router)
v1.include_router(indicators.router)
v1.include_router(signal.router)
v1.include_router(backtest.router)
v1.include_router(admin.router)
v1.include_router(paper_trading.router)
v1.include_router(experiment.router)
v1.include_router(research.router)
v1.include_router(studio.router)
app.include_router(v1)


__all__ = ["app"]
