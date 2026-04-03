from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_account_service
from src.api.error_codes import AIErrorAction, AIErrorCode, get_account_error_details
from src.api.schemas import (
    AccountInfoModel,
    ApiResponse,
    OrderModel,
    PositionModel,
    TradingAccountModel,
)
from src.clients.base import MT5TradeError
from src.trading.application import TradingQueryService

router = APIRouter(tags=["account"])


def _position_model_from_dataclass(position) -> PositionModel:
    payload = dict(position.__dict__)
    payload["time"] = position.time.isoformat()
    return PositionModel(**payload)


def _order_model_from_dataclass(order) -> OrderModel:
    payload = dict(order.__dict__)
    payload["time"] = order.time.isoformat()
    return OrderModel(**payload)


@router.get("/account/info", response_model=ApiResponse[AccountInfoModel])
def account_info(svc: TradingQueryService = Depends(get_account_service)) -> ApiResponse[AccountInfoModel]:
    active_alias = svc.active_account_alias
    try:
        info = svc.account_info()
        return ApiResponse.success_response(
            data=AccountInfoModel(**info.__dict__),
            metadata={
                "operation": "account_info",
                "login": info.login,
                "balance": info.balance,
                "equity": info.equity,
                "margin": info.margin,
                "free_margin": info.margin_free,
                "currency": info.currency,
                "leverage": info.leverage,
                "account_alias": active_alias,
                "status": "active" if info.balance > 0 else "inactive",
            },
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.ACCOUNT_INFO_FAILED,
            error_message=f"Get account info failed: {str(exc)}",
            suggested_action=AIErrorAction.CHECK_ACCOUNT_STATUS,
            details={
                "exception_type": type(exc).__name__,
                **get_account_error_details(operation="account_info"),
            },
        )
    except Exception as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.UNKNOWN_ERROR,
            error_message=f"Unknown error: {str(exc)}",
            suggested_action=AIErrorAction.CONTACT_SUPPORT,
            details={
                "exception_type": type(exc).__name__,
                **get_account_error_details(operation="account_info"),
            },
        )


@router.get("/account/positions", response_model=ApiResponse[List[PositionModel]])
def account_positions(
    symbol: Optional[str] = Query(default=None, description="symbol filter"),
    svc: TradingQueryService = Depends(get_account_service),
) -> ApiResponse[List[PositionModel]]:
    active_alias = svc.active_account_alias
    try:
        positions = svc.positions(symbol)
        items = [_position_model_from_dataclass(p) for p in positions]
        total_volume = sum(p.volume for p in positions)
        total_profit = sum(p.profit for p in positions) if positions and hasattr(positions[0], "profit") else 0
        buy_positions = [p for p in positions if p.type == 0]
        sell_positions = [p for p in positions if p.type == 1]
        return ApiResponse.success_response(
            data=items,
            metadata={
                "operation": "account_positions",
                "symbol_filter": symbol,
                "account_alias": active_alias,
                "count": len(items),
                "total_volume": total_volume,
                "total_profit": total_profit,
                "buy_count": len(buy_positions),
                "sell_count": len(sell_positions),
                "buy_volume": sum(p.volume for p in buy_positions),
                "sell_volume": sum(p.volume for p in sell_positions),
                "symbols": list(set(p.symbol for p in positions)) if positions else [],
            },
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.POSITION_NOT_FOUND,
            error_message=f"Get positions failed: {str(exc)}",
            suggested_action=AIErrorAction.CHECK_ACCOUNT_STATUS,
            details={
                "exception_type": type(exc).__name__,
                **get_account_error_details(operation="account_positions", symbol=symbol),
            },
        )
    except Exception as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.UNKNOWN_ERROR,
            error_message=f"Unknown error: {str(exc)}",
            suggested_action=AIErrorAction.CONTACT_SUPPORT,
            details={
                "exception_type": type(exc).__name__,
                **get_account_error_details(operation="account_positions", symbol=symbol),
            },
        )


@router.get("/account/orders", response_model=ApiResponse[List[OrderModel]])
def account_orders(
    symbol: Optional[str] = Query(default=None, description="symbol filter"),
    svc: TradingQueryService = Depends(get_account_service),
) -> ApiResponse[List[OrderModel]]:
    active_alias = svc.active_account_alias
    try:
        orders = svc.orders(symbol)
        items = [_order_model_from_dataclass(o) for o in orders]
        total_volume = sum(o.volume for o in orders)
        buy_orders = [o for o in orders if o.type == 0]
        sell_orders = [o for o in orders if o.type == 1]
        return ApiResponse.success_response(
            data=items,
            metadata={
                "operation": "account_orders",
                "symbol_filter": symbol,
                "account_alias": active_alias,
                "count": len(items),
                "total_volume": total_volume,
                "buy_count": len(buy_orders),
                "sell_count": len(sell_orders),
                "buy_volume": sum(o.volume for o in buy_orders),
                "sell_volume": sum(o.volume for o in sell_orders),
                "symbols": list(set(o.symbol for o in orders)) if orders else [],
            },
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.ORDER_NOT_FOUND,
            error_message=f"Get orders failed: {str(exc)}",
            suggested_action=AIErrorAction.CHECK_ACCOUNT_STATUS,
            details={
                "exception_type": type(exc).__name__,
                **get_account_error_details(operation="account_orders", symbol=symbol),
            },
        )
    except Exception as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.UNKNOWN_ERROR,
            error_message=f"Unknown error: {str(exc)}",
            suggested_action=AIErrorAction.CONTACT_SUPPORT,
            details={
                "exception_type": type(exc).__name__,
                **get_account_error_details(operation="account_orders", symbol=symbol),
            },
        )


@router.get("/account/list", response_model=ApiResponse[List[TradingAccountModel]])
def account_list(svc: TradingQueryService = Depends(get_account_service)) -> ApiResponse[List[TradingAccountModel]]:
    accounts = [TradingAccountModel(**item) for item in svc.list_accounts()]
    return ApiResponse.success_response(
        data=accounts,
        metadata={
            "operation": "account_list",
            "mode": "single_account_runtime",
            "active_account_alias": svc.active_account_alias,
            "count": len(accounts),
        },
    )
