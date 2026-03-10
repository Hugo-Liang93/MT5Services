from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_account_service
from src.api.schemas import AccountInfoModel, ApiResponse, OrderModel, PositionModel
from src.api.error_codes import AIErrorCode, AIErrorAction, get_account_error_details
from src.clients.mt5_trade import MT5TradeError
from src.core.account_service import AccountService

router = APIRouter(tags=["account"])


@router.get("/account/info", response_model=ApiResponse[AccountInfoModel])
def account_info(svc: AccountService = Depends(get_account_service)) -> ApiResponse[AccountInfoModel]:
    try:
        info = svc.account_info()
        
        return ApiResponse.success_response(
            data=AccountInfoModel(**info.__dict__),
            metadata={
                "operation": "account_info",
                "account_number": info.account_number,
                "balance": info.balance,
                "equity": info.equity,
                "margin": info.margin,
                "free_margin": info.free_margin,
                "margin_level": info.margin_level,
                "currency": info.currency,
                "leverage": info.leverage,
                "profit": info.profit,
                "status": "active" if info.balance > 0 else "inactive"
            }
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.ACCOUNT_INFO_FAILED,
            error_message=f"获取账户信息失败: {str(exc)}",
            suggested_action=AIErrorAction.CHECK_ACCOUNT_STATUS,
            details={
                "exception_type": type(exc).__name__,
                **get_account_error_details(operation="account_info")
            }
        )
    except Exception as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.UNKNOWN_ERROR,
            error_message=f"未知错误: {str(exc)}",
            suggested_action=AIErrorAction.CONTACT_SUPPORT,
            details={
                "exception_type": type(exc).__name__,
                **get_account_error_details(operation="account_info")
            }
        )


@router.get("/account/positions", response_model=ApiResponse[List[PositionModel]])
def account_positions(
    symbol: Optional[str] = Query(default=None, description="过滤品种，可为空表示全部"),
    svc: AccountService = Depends(get_account_service),
) -> ApiResponse[List[PositionModel]]:
    try:
        positions = svc.positions(symbol)
        items = [PositionModel(**p.__dict__, time=p.time.isoformat()) for p in positions]
        
        # 计算统计信息
        total_volume = sum(p.volume for p in positions)
        total_profit = sum(p.profit for p in positions) if positions and hasattr(positions[0], 'profit') else 0
        buy_positions = [p for p in positions if p.side == "buy"]
        sell_positions = [p for p in positions if p.side == "sell"]
        
        return ApiResponse.success_response(
            data=items,
            metadata={
                "operation": "account_positions",
                "symbol_filter": symbol,
                "count": len(items),
                "total_volume": total_volume,
                "total_profit": total_profit,
                "buy_count": len(buy_positions),
                "sell_count": len(sell_positions),
                "buy_volume": sum(p.volume for p in buy_positions),
                "sell_volume": sum(p.volume for p in sell_positions),
                "symbols": list(set(p.symbol for p in positions)) if positions else []
            }
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.POSITION_NOT_FOUND,
            error_message=f"获取持仓失败: {str(exc)}",
            suggested_action=AIErrorAction.CHECK_ACCOUNT_STATUS,
            details={
                "exception_type": type(exc).__name__,
                **get_account_error_details(operation="account_positions", symbol=symbol)
            }
        )
    except Exception as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.UNKNOWN_ERROR,
            error_message=f"未知错误: {str(exc)}",
            suggested_action=AIErrorAction.CONTACT_SUPPORT,
            details={
                "exception_type": type(exc).__name__,
                **get_account_error_details(operation="account_positions", symbol=symbol)
            }
        )


@router.get("/account/orders", response_model=ApiResponse[List[OrderModel]])
def account_orders(
    symbol: Optional[str] = Query(default=None, description="过滤品种，可为空表示全部"),
    svc: AccountService = Depends(get_account_service),
) -> ApiResponse[List[OrderModel]]:
    try:
        orders = svc.orders(symbol)
        items = [OrderModel(**o.__dict__, time=o.time.isoformat()) for o in orders]
        
        # 计算统计信息
        total_volume = sum(o.volume for o in orders)
        buy_orders = [o for o in orders if o.side == "buy"]
        sell_orders = [o for o in orders if o.side == "sell"]
        
        return ApiResponse.success_response(
            data=items,
            metadata={
                "operation": "account_orders",
                "symbol_filter": symbol,
                "count": len(items),
                "total_volume": total_volume,
                "buy_count": len(buy_orders),
                "sell_count": len(sell_orders),
                "buy_volume": sum(o.volume for o in buy_orders),
                "sell_volume": sum(o.volume for o in sell_orders),
                "symbols": list(set(o.symbol for o in orders)) if orders else []
            }
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.ORDER_NOT_FOUND,
            error_message=f"获取订单失败: {str(exc)}",
            suggested_action=AIErrorAction.CHECK_ACCOUNT_STATUS,
            details={
                "exception_type": type(exc).__name__,
                **get_account_error_details(operation="account_orders", symbol=symbol)
            }
        )
    except Exception as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.UNKNOWN_ERROR,
            error_message=f"未知错误: {str(exc)}",
            suggested_action=AIErrorAction.CONTACT_SUPPORT,
            details={
                "exception_type": type(exc).__name__,
                **get_account_error_details(operation="account_orders", symbol=symbol)
            }
        )