from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_runtime_read_model, get_trading_query_service
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse, OrderModel, PositionModel, TradingAccountModel
from src.clients.base import MT5TradeError
from src.readmodels.runtime import RuntimeReadModel
from src.trading.application import TradingQueryService

from .common import (
    normalize_optional_status,
    order_model_from_dataclass,
    position_model_from_dataclass,
)
from .view_models import (
    ExecutionContextListView,
    ExposureCloseoutSummaryView,
    PendingOrderStateListView,
    PositionRuntimeStateListView,
    TradeControlStatusView,
    TradeStateAlertsView,
    TradeStateSummaryView,
)

router = APIRouter(tags=["trade"])


@router.get("/trade/daily_summary", response_model=ApiResponse[dict])
def trade_daily_summary(
    service: TradingQueryService = Depends(get_trading_query_service),
) -> ApiResponse[dict]:
    return ApiResponse.success_response(
        data=service.daily_trade_summary(),
        metadata={
            "operation": "daily_summary",
            "account_alias": service.active_account_alias,
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


@router.get(
    "/trade/state/pending/active",
    response_model=ApiResponse[PendingOrderStateListView],
)
def trade_active_pending_state_list(
    limit: int = Query(default=50, ge=1, le=500),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[PendingOrderStateListView]:
    return ApiResponse.success_response(
        data=runtime_views.active_pending_order_payload(limit=limit),
        metadata={
            "operation": "trade_active_pending_state_list",
            "limit": limit,
        },
    )


@router.get(
    "/trade/state/execution-contexts",
    response_model=ApiResponse[ExecutionContextListView],
)
def trade_pending_execution_context_list(
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[ExecutionContextListView]:
    return ApiResponse.success_response(
        data=runtime_views.pending_execution_context_payload(),
        metadata={"operation": "trade_pending_execution_context_list"},
    )


@router.get(
    "/trade/state/pending/lifecycle",
    response_model=ApiResponse[PendingOrderStateListView],
)
def trade_pending_lifecycle_state_list(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[PendingOrderStateListView]:
    normalized_status = normalize_optional_status(status)
    statuses = [normalized_status] if normalized_status else None
    return ApiResponse.success_response(
        data=runtime_views.pending_order_state_payload(
            statuses=statuses,
            limit=limit,
        ),
        metadata={
            "operation": "trade_pending_lifecycle_state_list",
            "status": normalized_status,
            "limit": limit,
        },
    )


@router.get(
    "/trade/state/positions",
    response_model=ApiResponse[PositionRuntimeStateListView],
)
def trade_position_state_list(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[PositionRuntimeStateListView]:
    normalized_status = normalize_optional_status(status)
    statuses = [normalized_status] if normalized_status else None
    return ApiResponse.success_response(
        data=runtime_views.position_runtime_state_payload(
            statuses=statuses,
            limit=limit,
        ),
        metadata={
            "operation": "trade_position_state_list",
            "status": normalized_status,
            "limit": limit,
        },
    )


@router.get("/trade/entry_status", response_model=ApiResponse[dict])
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


@router.get("/positions", response_model=ApiResponse[List[PositionModel]])
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


@router.get("/orders", response_model=ApiResponse[List[OrderModel]])
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
) -> ApiResponse[List[TradingAccountModel]]:
    accounts = [TradingAccountModel(**item) for item in service.list_accounts()]
    return ApiResponse.success_response(
        data=accounts,
        metadata={
            "operation": "trading_accounts",
            "mode": "single_account_runtime",
            "active_account_alias": service.active_account_alias,
            "count": len(accounts),
        },
    )


@router.get("/trade/command-audits", response_model=ApiResponse[List[dict]])
def trade_command_audits(
    command_type: Optional[str] = Query(default=None, description="command type"),
    status: Optional[str] = Query(default=None, description="operation status"),
    limit: int = Query(default=100, ge=1, le=500),
    service: TradingQueryService = Depends(get_trading_query_service),
) -> ApiResponse[List[dict]]:
    items = service.recent_command_audits(
        command_type=command_type,
        status=status,
        limit=limit,
    )
    return ApiResponse.success_response(
        data=items,
        metadata={
            "operation": "trade_command_audits",
            "account_alias": service.active_account_alias,
            "command_type": command_type,
            "status": status,
            "count": len(items),
        },
    )
