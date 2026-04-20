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
    execution,
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


def _safe_component_snapshot(label: str, getter) -> dict:
    try:
        return getter()
    except Exception as exc:
        logger.debug("Health snapshot failed for %s", label, exc_info=True)
        return {"status": "error", "error": str(exc)}


def _default_paper_trading_summary() -> dict:
    return {
        "kind": "validation_sidecar",
        "configured": False,
        "running": False,
        "status": "disabled",
        "session_id": None,
        "signals_received": 0,
        "signals_executed": 0,
        "signals_rejected": 0,
        "reject_reasons": {},
        "active_symbols": [],
    }


def _default_mt5_session_summary() -> dict:
    return {
        "kind": "external_dependency",
        "status": "unavailable",
        "connected": False,
        "terminal_reachable": False,
        "terminal_process_ready": False,
        "ipc_ready": False,
        "authorized": False,
        "account_match": False,
        "session_ready": False,
        "interactive_login_required": False,
        "error_code": "runtime_read_model_unavailable",
        "error_message": "runtime read model does not expose mt5_session_summary",
        "last_error": {"code": None, "message": None},
    }


def _safe_optional_snapshot(target, attr: str, *, fallback: dict) -> dict:
    getter = getattr(target, attr, None)
    if not callable(getter):
        return dict(fallback)
    try:
        return getter()
    except Exception:
        logger.debug("Health snapshot failed for %s", attr, exc_info=True)
        return dict(fallback)


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
    runtime_read_model=Depends(deps.get_runtime_read_model),
) -> ApiResponse[dict]:
    try:
        market_status = service.health()
    except MT5MarketError as exc:
        market_status = {"connected": False, "error": str(exc)}
    trading_status = trading.health()
    runtime_identity = getattr(runtime_read_model, "runtime_identity", None)
    is_executor = bool(
        runtime_identity is not None
        and getattr(runtime_identity, "instance_role", None) == "executor"
    )
    storage_summary = runtime_read_model.storage_summary()
    queues = (
        {
            "summary": storage_summary.get("summary", {}),
            "threads": storage_summary.get("threads", {}),
            "role": "executor",
            "ingestion": "disabled",
        }
        if is_executor
        else deps.get_ingestor().queue_stats()
    )
    indicator_summary = runtime_read_model.indicator_summary()
    signal_summary = runtime_read_model.signal_runtime_summary()
    executor_summary = runtime_read_model.trade_executor_summary()
    pending_summary = runtime_read_model.pending_entries_summary()
    position_summary = runtime_read_model.position_manager_summary()
    trade_executor_running = bool(
        executor_summary.get("running", executor_summary.get("enabled", False))
    )
    pending_running = bool(
        pending_summary.get(
            "running",
            pending_summary.get("status")
            not in {"critical", "disabled", "unavailable"},
        )
    )
    position_running = bool(position_summary.get("running", False))
    runtime_components = {
        "ingestor": {
            "running": bool(queues.get("threads", {}).get("ingest_alive", False)),
            "status": "disabled" if is_executor else "enabled",
            "intrabar_synthesis": dict(
                storage_summary.get("intrabar_synthesis", {}) or {}
            ),
        },
        "storage_writer": {
            "running": bool(queues.get("threads", {}).get("writer_alive", False)),
            "status": storage_summary.get("status"),
        },
        "indicator_engine": {
            "running": bool(indicator_summary.get("event_loop_running", False)),
            "status": indicator_summary.get("status"),
        },
        "signal_runtime": {
            "running": bool(signal_summary.get("running", False)),
            "status": signal_summary.get("status"),
        },
        "trade_executor": {
            "configured": bool(executor_summary.get("configured", False)),
            "armed": bool(
                executor_summary.get("armed", executor_summary.get("enabled", False))
            ),
            "running": trade_executor_running,
            "status": executor_summary.get("status"),
        },
        "pending_entry_manager": {
            "configured": bool(pending_summary.get("configured", False)),
            "running": pending_running,
            "status": pending_summary.get("status"),
        },
        "position_manager": {
            "configured": bool(position_summary.get("configured", False)),
            "running": position_running,
            "status": position_summary.get("status"),
        },
        "economic_calendar": _safe_component_snapshot(
            "economic_calendar",
            lambda: deps.get_economic_calendar_service().stats(),
        ),
    }
    validation_sidecars = {
        "paper_trading": _safe_optional_snapshot(
            runtime_read_model,
            "paper_trading_summary",
            fallback=_default_paper_trading_summary(),
        ),
    }
    market_mt5_session = None
    if isinstance(market_status, dict):
        session_payload = market_status.get("mt5_session")
        if isinstance(session_payload, dict):
            market_mt5_session = dict(session_payload)
            market_mt5_session.setdefault("kind", "external_dependency")
            market_mt5_session.setdefault(
                "status",
                "healthy" if market_mt5_session.get("session_ready") else "critical",
            )
            market_mt5_session.setdefault(
                "connected",
                bool(market_mt5_session.get("session_ready")),
            )
    external_dependencies = {
        "mt5_session": market_mt5_session
        or _safe_optional_snapshot(
            runtime_read_model,
            "mt5_session_summary",
            fallback=_default_mt5_session_summary(),
        ),
    }

    return ApiResponse(
        success=True,
        data={
            "mode": deps.get_runtime_mode(),
            "market": market_status,
            "trading": trading_status,
            "ingestor": {"queues": queues},
            "runtime": {
                "components": runtime_components,
                "validation_sidecars": validation_sidecars,
                "external_dependencies": external_dependencies,
            },
        },
    )


v1 = APIRouter(prefix="/v1")
v1.include_router(market.router)
v1.include_router(decision.router)
v1.include_router(economic.router)
v1.include_router(account.router)
v1.include_router(trade.router)
v1.include_router(execution.router)
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
