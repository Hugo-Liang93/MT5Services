from __future__ import annotations

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import get_runtime_read_model, get_trading_query_service
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse, OrderModel, PositionModel, TradingAccountModel
from src.clients.base import MT5TradeError
from src.config import build_account_key, list_mt5_accounts, resolve_current_environment
from src.readmodels.runtime import RuntimeReadModel
from src.trading.application.services import TradingQueryService

from ..common import (
    normalize_optional_status,
    order_model_from_dataclass,
    position_model_from_dataclass,
)
from ..view_models import (
    ExposureCloseoutSummaryView,
    TradeControlStatusView,
    TradeDailySummaryView,
    TradeEntryStatusView,
    TradeStateAlertsView,
    TradeStateSummaryView,
)

router = APIRouter(tags=["trade"])


@router.get("/trade/daily_summary", response_model=ApiResponse[TradeDailySummaryView])
def trade_daily_summary(
    summary_date: Optional[date] = Query(
        default=None,
        alias="date",
        description="报告日期 (YYYY-MM-DD)，缺省 = 今天",
    ),
    service: TradingQueryService = Depends(get_trading_query_service),
) -> ApiResponse[dict]:
    return ApiResponse.success_response(
        data=service.daily_trade_summary(summary_date=summary_date),
        metadata={
            "operation": "daily_summary",
            "account_alias": service.active_account_alias,
            "summary_date": summary_date.isoformat() if summary_date else None,
        },
    )


@router.get("/trade/control", response_model=ApiResponse[TradeControlStatusView])
def trade_control_status(
    service: TradingQueryService = Depends(get_trading_query_service),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[TradeControlStatusView]:
    payload = {
        "trade_control": service.trade_control_status(),
        "persisted_trade_control": runtime_views.persisted_trade_control_payload(),
        "trading_state": runtime_views.trading_state_summary(
            pending_limit=10,
            position_limit=10,
        ),
        "executor": runtime_views.trade_executor_summary(),
    }
    return ApiResponse.success_response(
        data=payload,
        metadata={"operation": "trade_control_status"},
    )


@router.get("/trade/state", response_model=ApiResponse[TradeStateSummaryView])
def trade_state_summary(
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[TradeStateSummaryView]:
    return ApiResponse.success_response(
        data=runtime_views.trading_state_summary(
            pending_limit=20,
            position_limit=20,
        ),
        metadata={"operation": "trade_state_summary"},
    )


@router.get("/trade/state/alerts", response_model=ApiResponse[TradeStateAlertsView])
def trade_state_alerts_summary(
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[TradeStateAlertsView]:
    return ApiResponse.success_response(
        data=runtime_views.trading_state_alerts_summary(),
        metadata={"operation": "trade_state_alerts_summary"},
    )


@router.get(
    "/trade/state/closeout",
    response_model=ApiResponse[ExposureCloseoutSummaryView],
)
def trade_state_closeout_summary(
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[ExposureCloseoutSummaryView]:
    return ApiResponse.success_response(
        data=runtime_views.exposure_closeout_summary(),
        metadata={"operation": "trade_state_closeout_summary"},
    )


@router.get("/trade/entry_status", response_model=ApiResponse[TradeEntryStatusView])
def trade_entry_status(
    symbol: Optional[str] = Query(default=None),
    volume: float = Query(default=0.1, gt=0),
    side: str = Query(default="buy"),
    order_kind: str = Query(default="market"),
    service: TradingQueryService = Depends(get_trading_query_service),
) -> ApiResponse[dict]:
    return ApiResponse.success_response(
        data=service.entry_to_order_status(
            symbol=symbol,
            volume=volume,
            side=side,
            order_kind=order_kind,
        ),
        metadata={
            "operation": "entry_status",
            "account_alias": service.active_account_alias,
        },
    )


@router.get(
    "/positions",
    response_model=ApiResponse[List[PositionModel]],
    deprecated=True,
    summary="[已弃用] 改用 GET /v1/execution/workbench (positions 块)",
    description=(
        "**已弃用** — 与 `/v1/execution/workbench?include=positions` 重复。"
        "新代码统一调用 workbench；保留兼容期 1 个月（2026-06-01 后下线）。\n\n"
        "差异：本端点仅返回 service 缓存的扁平列表；workbench.positions 块带 "
        "`status_counts` / `positions_updated_at` / source_kind。\n\n"
        "若需直查 MT5 实时数据（绕过 service），改用 `/v1/account/positions`。"
    ),
)
def positions(
    symbol: Optional[str] = Query(default=None, description="trading symbol"),
    magic: Optional[int] = Query(default=None, description="magic id"),
    service: TradingQueryService = Depends(get_trading_query_service),
) -> ApiResponse[List[PositionModel]]:
    active_alias = service.active_account_alias
    try:
        rows = service.get_positions(symbol, magic)
        items = [position_model_from_dataclass(p) for p in rows]
        return ApiResponse.success_response(
            data=items,
            metadata={
                "operation": "get_positions",
                "account_alias": active_alias,
                "symbol": symbol,
                "magic": magic,
                "count": len(items),
                "total_volume": sum(p.volume for p in rows),
                "total_profit": (
                    sum(p.profit for p in rows)
                    if rows and hasattr(rows[0], "profit")
                    else None
                ),
                "deprecated": True,
                "deprecation": {
                    "successor": "/v1/execution/workbench",
                    "successor_query": "include=positions",
                    "sunset": "2026-06-01",
                    "reason": "duplicate_of_workbench_block",
                },
            },
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.POSITION_NOT_FOUND,
            error_message=f"Get positions error: {str(exc)}",
            suggested_action=AIErrorAction.CHECK_ACCOUNT_STATUS,
            details={
                "exception_type": type(exc).__name__,
                "account_alias": active_alias,
                "symbol": symbol,
                "magic": magic,
            },
        )


@router.get(
    "/orders",
    response_model=ApiResponse[List[OrderModel]],
    deprecated=True,
    summary="[已弃用] 改用 GET /v1/execution/workbench (orders 块)",
    description=(
        "**已弃用** — 与 `/v1/execution/workbench?include=orders` 重复。"
        "新代码统一调用 workbench；保留兼容期 1 个月（2026-06-01 后下线）。\n\n"
        "若需直查 MT5 实时挂单（绕过 service），改用 `/v1/account/orders`。"
    ),
)
def orders(
    symbol: Optional[str] = Query(default=None, description="trading symbol"),
    magic: Optional[int] = Query(default=None, description="magic id"),
    service: TradingQueryService = Depends(get_trading_query_service),
) -> ApiResponse[List[OrderModel]]:
    active_alias = service.active_account_alias
    try:
        rows = service.get_orders(symbol, magic)
        items = [order_model_from_dataclass(o) for o in rows]
        return ApiResponse.success_response(
            data=items,
            metadata={
                "operation": "get_orders",
                "account_alias": active_alias,
                "symbol": symbol,
                "magic": magic,
                "count": len(items),
                "total_volume": sum(o.volume for o in rows),
                "deprecated": True,
                "deprecation": {
                    "successor": "/v1/execution/workbench",
                    "successor_query": "include=orders",
                    "sunset": "2026-06-01",
                    "reason": "duplicate_of_workbench_block",
                },
            },
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.ORDER_NOT_FOUND,
            error_message=f"Get orders error: {str(exc)}",
            suggested_action=AIErrorAction.CHECK_ACCOUNT_STATUS,
            details={
                "exception_type": type(exc).__name__,
                "account_alias": active_alias,
                "symbol": symbol,
                "magic": magic,
            },
        )


@router.get("/trade/accounts", response_model=ApiResponse[List[TradingAccountModel]])
def trading_accounts(
    service: TradingQueryService = Depends(get_trading_query_service),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[List[TradingAccountModel]]:
    risk_payload = runtime_views.account_risk_states_payload(limit=500)
    risk_by_account_key = {
        str(item.get("account_key") or ""): item
        for item in list(risk_payload.get("items") or [])
        if str(item.get("account_key") or "").strip()
    }
    configured_accounts = []
    active_account_alias = service.active_account_alias
    environment = (
        runtime_views.runtime_identity.environment
        if runtime_views.runtime_identity is not None
        else resolve_current_environment()
    )
    if environment is None:
        raise HTTPException(
            status_code=500, detail="runtime environment is not configured"
        )
    for settings in list_mt5_accounts():
        account_key = build_account_key(
            environment,
            settings.mt5_server,
            settings.mt5_login,
        )
        risk_state = risk_by_account_key.get(account_key)
        tradability = None
        unmanaged = None
        if settings.account_alias == active_account_alias:
            tradability = runtime_views.tradability_state_summary()
            unmanaged = runtime_views.unmanaged_live_positions_payload(limit=20)
        elif isinstance(risk_state, dict):
            auto_entry_enabled = bool(risk_state.get("auto_entry_enabled", True))
            close_only_mode = bool(risk_state.get("close_only_mode", False))
            circuit_open = bool(risk_state.get("circuit_open", False))
            should_block_new_trades = bool(
                risk_state.get("should_block_new_trades", False)
            )
            quote_stale = bool(risk_state.get("quote_stale", False))
            tradability = {
                "runtime_present": True,
                "admission_enabled": auto_entry_enabled and not close_only_mode,
                "market_data_fresh": not quote_stale,
                "quote_health": {
                    "stale": quote_stale,
                    "age_seconds": (risk_state.get("metadata", {}) or {})
                    .get("quote_health", {})
                    .get("age_seconds"),
                    "stale_threshold_seconds": (risk_state.get("metadata", {}) or {})
                    .get("quote_health", {})
                    .get("stale_threshold_seconds"),
                },
                "session_allowed": {"status": "unknown", "reason": None},
                "economic_guard": {"status": "warn_only", "degraded": False},
                "auto_entry_enabled": auto_entry_enabled,
                "close_only_mode": close_only_mode,
                "margin_guard": (risk_state.get("metadata", {}) or {}).get(
                    "margin_guard", {}
                ),
                "circuit_open": circuit_open,
                "tradable": bool(
                    auto_entry_enabled
                    and not close_only_mode
                    and not circuit_open
                    and not should_block_new_trades
                    and not quote_stale
                ),
            }
            unmanaged = {"count": 0, "items": [], "reason_counts": {}}
        configured_accounts.append(
            {
                "alias": settings.account_alias,
                "label": settings.account_label or settings.account_alias,
                "account_key": account_key,
                "login": settings.mt5_login,
                "server": settings.mt5_server,
                "environment": environment,
                "timezone": settings.timezone,
                "enabled": settings.enabled,
                "default": settings.account_alias == active_account_alias,
                "active": settings.account_alias == active_account_alias,
                "risk_state": risk_state,
                "tradability": tradability,
                "unmanaged_live_positions": unmanaged,
            }
        )
    accounts = [TradingAccountModel(**item) for item in configured_accounts]
    return ApiResponse.success_response(
        data=accounts,
        metadata={
            "operation": "trading_accounts",
            "mode": "account_risk_projection",
            "active_account_alias": active_account_alias,
            "count": len(accounts),
        },
    )
