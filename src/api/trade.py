from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_signal_service, get_trading_service
from src.api.trade_dispatcher import TradeAPIDispatcher
from src.api.error_codes import AIErrorAction, AIErrorCode, get_trade_error_details
from src.api.schemas import (
    ApiResponse,
    BatchCancelOrdersRequest,
    BatchCloseRequest,
    BatchTradeRequest,
    CancelOrdersRequest,
    CloseAllRequest,
    CloseRequest,
    EstimateMarginRequest,
    ModifyOrdersRequest,
    ModifyPositionsRequest,
    OrderModel,
    PositionModel,
    TradePrecheckModel,
    TradeRequest,
    TradeDispatchRequest,
    SignalExecuteTradeRequest,
    TradingAccountModel,
)
from src.clients.base import MT5TradeError
from src.risk.service import PreTradeRiskBlockedError
from src.signals.service import SignalModule
from src.signals.execution.sizing import compute_trade_params, extract_atr_from_indicators
from src.trading.service import TradingModule

router = APIRouter(tags=["trade"])


def _trade_request_details(request: TradeRequest) -> dict:
    return get_trade_error_details(
        symbol=request.symbol,
        volume=request.volume,
        side=request.side,
        price=request.price,
    )


@router.post("/trade/dispatch", response_model=ApiResponse[dict])
def trade_dispatch(
    request: TradeDispatchRequest,
    service: TradingModule = Depends(get_trading_service),
) -> ApiResponse[dict]:
    dispatcher = TradeAPIDispatcher(service)
    return dispatcher.dispatch(request.operation, request.payload)


@router.get("/trade/daily_summary", response_model=ApiResponse[dict])
def trade_daily_summary(
    service: TradingModule = Depends(get_trading_service),
) -> ApiResponse[dict]:
    return ApiResponse.success_response(
        data=service.daily_trade_summary(),
        metadata={
            "operation": "daily_summary",
            "account_alias": service.active_account_alias,
        },
    )


@router.get("/trade/entry_status", response_model=ApiResponse[dict])
def trade_entry_status(
    symbol: Optional[str] = Query(default=None),
    volume: float = Query(default=0.1, gt=0),
    side: str = Query(default="buy"),
    order_kind: str = Query(default="market"),
    service: TradingModule = Depends(get_trading_service),
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


@router.post("/trade/precheck", response_model=ApiResponse[TradePrecheckModel])
def trade_precheck(
    request: TradeRequest,
    service: TradingModule = Depends(get_trading_service),
) -> ApiResponse[TradePrecheckModel]:
    active_alias = service.active_account_alias
    try:
        result = service.precheck_trade(
            symbol=request.symbol,
            volume=request.volume,
            side=request.side,
            order_kind=request.order_kind,
            price=request.price,
            sl=request.sl,
            tp=request.tp,
            deviation=request.deviation,
            comment=request.comment,
            magic=request.magic,
        )
        request_id = result.get("request_id") if isinstance(result, dict) else None
        return ApiResponse.success_response(
            data=TradePrecheckModel(**result),
            metadata={
                "operation": "trade_precheck",
                "account_alias": active_alias,
                "symbol": request.symbol,
                "side": request.side,
                "volume": request.volume,
                "request_id": request_id,
            },
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.TRADE_EXECUTION_FAILED,
            error_message=f"Trade precheck failed: {str(exc)}",
            suggested_action=AIErrorAction.CHECK_ACCOUNT_STATUS,
            details={
                "exception_type": type(exc).__name__,
                "account_alias": active_alias,
                **_trade_request_details(request),
            },
        )
    except Exception as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.UNKNOWN_ERROR,
            error_message=f"Trade precheck failed: {str(exc)}",
            suggested_action=AIErrorAction.CONTACT_SUPPORT,
            details={
                "exception_type": type(exc).__name__,
                "account_alias": active_alias,
                **_trade_request_details(request),
            },
        )


@router.post("/trade", response_model=ApiResponse[dict])
def trade(
    request: TradeRequest,
    service: TradingModule = Depends(get_trading_service),
) -> ApiResponse[dict]:
    active_alias = service.active_account_alias
    try:
        result = service.execute_trade(
            symbol=request.symbol,
            volume=request.volume,
            side=request.side,
            order_kind=request.order_kind,
            price=request.price,
            sl=request.sl,
            tp=request.tp,
            deviation=request.deviation,
            comment=request.comment,
            magic=request.magic,
            dry_run=request.dry_run,
            request_id=request.request_id,
        )
        if result is None:
            return ApiResponse.error_response(
                error_code=AIErrorCode.TRADE_EXECUTION_FAILED,
                error_message="Trade execution failed",
                suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
                details=_trade_request_details(request),
            )
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "trade_execution",
                "account_alias": active_alias,
                "symbol": request.symbol,
                "side": request.side,
                "order_kind": request.order_kind,
                "volume": request.volume,
                "price": result.get("price") if result else None,
                "ticket": result.get("ticket") if result else None,
                "dry_run": request.dry_run,
                "request_id": result.get("request_id") if result else request.request_id,
                "trace_id": result.get("trace_id") if result else None,
                "operation_id": result.get("operation_id") if result else None,
            },
        )
    except PreTradeRiskBlockedError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.TRADE_BLOCKED_BY_RISK,
            error_message=str(exc),
            suggested_action=AIErrorAction.WAIT_FOR_RISK_WINDOW,
            details={
                **_trade_request_details(request),
                "risk_assessment": exc.assessment,
                "account_alias": active_alias,
            },
        )
    except MT5TradeError as exc:
        error_msg = str(exc)
        if "insufficient margin" in error_msg.lower():
            error_code = AIErrorCode.INSUFFICIENT_MARGIN
            suggested_action = AIErrorAction.REDUCE_VOLUME
        elif "invalid volume" in error_msg.lower():
            error_code = AIErrorCode.INVALID_VOLUME
            suggested_action = AIErrorAction.ADJUST_PRICE
        elif "market closed" in error_msg.lower():
            error_code = AIErrorCode.MARKET_CLOSED
            suggested_action = AIErrorAction.WAIT_FOR_MARKET_OPEN
        elif "price" in error_msg.lower():
            error_code = AIErrorCode.INVALID_PRICE
            suggested_action = AIErrorAction.USE_MARKET_ORDER
        else:
            error_code = AIErrorCode.TRADE_EXECUTION_FAILED
            suggested_action = AIErrorAction.RETRY_AFTER_DELAY
        return ApiResponse.error_response(
            error_code=error_code,
            error_message=f"Trade error: {error_msg}",
            suggested_action=suggested_action,
            details={
                "exception_type": type(exc).__name__,
                "account_alias": active_alias,
                **_trade_request_details(request),
            },
        )


@router.post("/trade/from-signal", response_model=ApiResponse[dict])
def trade_from_signal(
    request: SignalExecuteTradeRequest,
    signal_service: SignalModule = Depends(get_signal_service),
    service: TradingModule = Depends(get_trading_service),
) -> ApiResponse[dict]:
    """从 confirmed 信号触发交易（职责归属交易模块）。"""
    rows = signal_service.recent_signals(scope="confirmed", limit=500)
    signal_row = next((row for row in rows if row.get("signal_id") == request.signal_id), None)
    if signal_row is None:
        return ApiResponse.error_response(
            error_code=AIErrorCode.DATA_NOT_AVAILABLE,
            error_message=f"Signal not found: {request.signal_id}",
            suggested_action=AIErrorAction.USE_FALLBACK_DATA,
            details={"signal_id": request.signal_id},
        )

    action = signal_row.get("action", "")
    if action not in {"buy", "sell"}:
        return ApiResponse.error_response(
            error_code=AIErrorCode.VALIDATION_ERROR,
            error_message=f"Signal action '{action}' is not executable (buy/sell required)",
            suggested_action=AIErrorAction.VALIDATE_PARAMETERS,
            details={"signal_id": request.signal_id, "action": action},
        )

    indicators = signal_row.get("indicators_snapshot") or {}
    atr = extract_atr_from_indicators(indicators)
    if atr is None or atr <= 0:
        return ApiResponse.error_response(
            error_code=AIErrorCode.INSUFFICIENT_HISTORY_DATA,
            error_message="Cannot compute trade params: ATR not found in signal snapshot",
            suggested_action=AIErrorAction.WAIT_FOR_DATA,
            details={"signal_id": request.signal_id},
        )

    try:
        account = service.account_info() or {}
        equity = account.get("equity") if isinstance(account, dict) else getattr(account, "equity", None)
        balance_value = account.get("balance") if isinstance(account, dict) else getattr(account, "balance", None)
        balance = float(equity or balance_value or 0)
    except Exception:
        balance = 0.0
    if balance <= 0:
        return ApiResponse.error_response(
            error_code=AIErrorCode.ACCOUNT_INFO_FAILED,
            error_message="Cannot compute position size: account balance unavailable",
            suggested_action=AIErrorAction.CHECK_ACCOUNT_STATUS,
            details={"signal_id": request.signal_id},
        )

    entry_price: Optional[float] = None
    for ind_name in ("bollinger20", "sma20", "close", "price"):
        payload = indicators.get(ind_name)
        if isinstance(payload, dict):
            for field in ("close", "value", "last", "bb_mid", "sma"):
                val = payload.get(field)
                if val is not None:
                    try:
                        entry_price = float(val)
                        break
                    except (TypeError, ValueError):
                        continue
        if entry_price:
            break
    if entry_price is None or entry_price <= 0:
        return ApiResponse.error_response(
            error_code=AIErrorCode.DATA_NOT_AVAILABLE,
            error_message="Cannot estimate entry price from signal snapshot",
            suggested_action=AIErrorAction.WAIT_FOR_DATA,
            details={"signal_id": request.signal_id},
        )

    params = compute_trade_params(
        action=action,
        current_price=entry_price,
        atr_value=atr,
        account_balance=balance,
    )
    volume = request.volume_override if request.volume_override is not None else params.position_size
    payload = {
        "symbol": signal_row.get("symbol"),
        "volume": volume,
        "side": action,
        "order_kind": "market",
        "sl": params.stop_loss,
        "tp": params.take_profit,
        "comment": f"agent:{signal_row.get('strategy')}:{action}:{request.signal_id[:8]}",
    }
    result = service.dispatch_operation("trade", payload)
    return ApiResponse.success_response(
        data=result if isinstance(result, dict) else {"result": result},
        metadata={
            "operation": "trade_from_signal",
            "signal_id": request.signal_id,
            "action": action,
            "volume": volume,
            "sl": params.stop_loss,
            "tp": params.take_profit,
            "risk_reward_ratio": params.risk_reward_ratio,
            "account_alias": service.active_account_alias,
        },
    )


@router.post("/close", response_model=ApiResponse[dict])
def close(
    request: CloseRequest,
    service: TradingModule = Depends(get_trading_service),
) -> ApiResponse[dict]:
    active_alias = service.active_account_alias
    try:
        result = service.close_position(
            ticket=request.ticket,
            volume=request.volume,
            deviation=request.deviation,
            comment=request.comment,
        )
        if result is None:
            return ApiResponse.error_response(
                error_code=AIErrorCode.TRADE_CLOSE_FAILED,
                error_message="Close position failed",
                suggested_action=AIErrorAction.CLOSE_POSITION,
                details={"ticket": request.ticket, "account_alias": active_alias},
            )
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "position_close",
                "account_alias": active_alias,
                "ticket": request.ticket,
                "success": result.get("success") if result else None,
                "volume": result.get("volume") if result else None,
            },
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.TRADE_CLOSE_FAILED,
            error_message=f"Close position error: {str(exc)}",
            suggested_action=AIErrorAction.CLOSE_POSITION,
            details={
                "exception_type": type(exc).__name__,
                "account_alias": active_alias,
                "ticket": request.ticket,
                "volume": request.volume,
            },
        )


@router.post("/trade/batch", response_model=ApiResponse[dict])
def trade_batch(
    request: BatchTradeRequest,
    service: TradingModule = Depends(get_trading_service),
) -> ApiResponse[dict]:
    active_alias = service.active_account_alias
    result = service.execute_trade_batch(
        trades=[trade.model_dump() for trade in request.trades],
        stop_on_error=request.stop_on_error,
    )
    return ApiResponse.success_response(
        data=result,
        metadata={
            "operation": "trade_batch",
            "account_alias": active_alias,
            "count": len(request.trades),
            "stop_on_error": request.stop_on_error,
        },
    )


@router.post("/close_all", response_model=ApiResponse[dict])
def close_all(
    request: CloseAllRequest,
    service: TradingModule = Depends(get_trading_service),
) -> ApiResponse[dict]:
    active_alias = service.active_account_alias
    try:
        result = service.close_all_positions(
            symbol=request.symbol,
            magic=request.magic,
            side=request.side,
            deviation=request.deviation,
            comment=request.comment,
        )
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "close_all_positions",
                "account_alias": active_alias,
                "symbol": request.symbol,
                "magic": request.magic,
                "side": request.side,
                "closed_count": len(result.get("closed", [])),
                "failed_count": len(result.get("failed", [])),
            },
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.TRADE_CLOSE_FAILED,
            error_message=f"Close all positions error: {str(exc)}",
            suggested_action=AIErrorAction.CLOSE_POSITION,
            details={
                "exception_type": type(exc).__name__,
                "account_alias": active_alias,
                "symbol": request.symbol,
                "magic": request.magic,
            },
        )


@router.post("/close/batch", response_model=ApiResponse[dict])
def close_batch(
    request: BatchCloseRequest,
    service: TradingModule = Depends(get_trading_service),
) -> ApiResponse[dict]:
    active_alias = service.active_account_alias
    try:
        result = service.close_positions_by_tickets(
            tickets=request.tickets,
            deviation=request.deviation,
            comment=request.comment,
        )
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "close_batch",
                "account_alias": active_alias,
                "count": len(request.tickets),
                "closed_count": len(result.get("closed", [])),
                "failed_count": len(result.get("failed", [])),
            },
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.TRADE_CLOSE_FAILED,
            error_message=f"Batch close error: {str(exc)}",
            suggested_action=AIErrorAction.CLOSE_POSITION,
            details={
                "exception_type": type(exc).__name__,
                "account_alias": active_alias,
                "tickets": request.tickets,
            },
        )


@router.post("/cancel_orders", response_model=ApiResponse[dict])
def cancel_orders(
    request: CancelOrdersRequest,
    service: TradingModule = Depends(get_trading_service),
) -> ApiResponse[dict]:
    active_alias = service.active_account_alias
    try:
        result = service.cancel_orders(
            symbol=request.symbol,
            magic=request.magic,
        )
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "cancel_orders",
                "account_alias": active_alias,
                "symbol": request.symbol,
                "magic": request.magic,
                "canceled_count": len(result.get("canceled", [])),
                "failed_count": len(result.get("failed", [])),
            },
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.TRADE_CANCEL_FAILED,
            error_message=f"Cancel orders error: {str(exc)}",
            suggested_action=AIErrorAction.CANCEL_ORDER,
            details={
                "exception_type": type(exc).__name__,
                "account_alias": active_alias,
                "symbol": request.symbol,
                "magic": request.magic,
            },
        )


@router.post("/cancel_orders/batch", response_model=ApiResponse[dict])
def cancel_orders_batch(
    request: BatchCancelOrdersRequest,
    service: TradingModule = Depends(get_trading_service),
) -> ApiResponse[dict]:
    active_alias = service.active_account_alias
    try:
        result = service.cancel_orders_by_tickets(request.tickets)
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "cancel_orders_batch",
                "account_alias": active_alias,
                "count": len(request.tickets),
                "canceled_count": len(result.get("canceled", [])),
                "failed_count": len(result.get("failed", [])),
            },
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.TRADE_CANCEL_FAILED,
            error_message=f"Batch cancel error: {str(exc)}",
            suggested_action=AIErrorAction.CANCEL_ORDER,
            details={
                "exception_type": type(exc).__name__,
                "account_alias": active_alias,
                "tickets": request.tickets,
            },
        )


@router.post("/estimate_margin", response_model=ApiResponse[dict])
def estimate_margin(
    request: EstimateMarginRequest,
    service: TradingModule = Depends(get_trading_service),
) -> ApiResponse[dict]:
    active_alias = service.active_account_alias
    try:
        result = service.estimate_margin(
            symbol=request.symbol,
            volume=request.volume,
            side=request.side,
            price=request.price,
        )
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "estimate_margin",
                "account_alias": active_alias,
                "symbol": request.symbol,
                "volume": request.volume,
                "side": request.side,
                "price": request.price,
            },
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.INSUFFICIENT_MARGIN,
            error_message=f"Margin estimate error: {str(exc)}",
            suggested_action=AIErrorAction.REDUCE_VOLUME,
            details={
                "exception_type": type(exc).__name__,
                "account_alias": active_alias,
                **_trade_request_details(
                    TradeRequest(
                        symbol=request.symbol,
                        volume=request.volume,
                        side=request.side,
                        price=request.price,
                    )
                ),
            },
        )


@router.put("/modify_orders", response_model=ApiResponse[dict])
def modify_orders(
    request: ModifyOrdersRequest,
    service: TradingModule = Depends(get_trading_service),
) -> ApiResponse[dict]:
    active_alias = service.active_account_alias
    try:
        result = service.modify_orders(
            symbol=request.symbol,
            magic=request.magic,
            sl=request.sl,
            tp=request.tp,
        )
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "modify_orders",
                "account_alias": active_alias,
                "symbol": request.symbol,
                "magic": request.magic,
                "modified_count": len(result.get("modified", [])),
                "failed_count": len(result.get("failed", [])),
            },
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.TRADE_MODIFICATION_FAILED,
            error_message=f"Modify orders error: {str(exc)}",
            suggested_action=AIErrorAction.MODIFY_POSITION,
            details={
                "exception_type": type(exc).__name__,
                "account_alias": active_alias,
                "symbol": request.symbol,
                "magic": request.magic,
            },
        )


@router.put("/modify_positions", response_model=ApiResponse[dict])
def modify_positions(
    request: ModifyPositionsRequest,
    service: TradingModule = Depends(get_trading_service),
) -> ApiResponse[dict]:
    active_alias = service.active_account_alias
    try:
        result = service.modify_positions(
            symbol=request.symbol,
            magic=request.magic,
            sl=request.sl,
            tp=request.tp,
        )
        return ApiResponse.success_response(
            data=result,
            metadata={
                "operation": "modify_positions",
                "account_alias": active_alias,
                "symbol": request.symbol,
                "magic": request.magic,
                "modified_count": len(result.get("modified", [])),
                "failed_count": len(result.get("failed", [])),
            },
        )
    except MT5TradeError as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.TRADE_MODIFICATION_FAILED,
            error_message=f"Modify positions error: {str(exc)}",
            suggested_action=AIErrorAction.MODIFY_POSITION,
            details={
                "exception_type": type(exc).__name__,
                "account_alias": active_alias,
                "symbol": request.symbol,
                "magic": request.magic,
            },
        )


@router.get("/positions", response_model=ApiResponse[List[PositionModel]])
def positions(
    symbol: Optional[str] = Query(default=None, description="trading symbol"),
    magic: Optional[int] = Query(default=None, description="magic id"),
    service: TradingModule = Depends(get_trading_service),
) -> ApiResponse[List[PositionModel]]:
    active_alias = service.active_account_alias
    try:
        positions = service.get_positions(symbol, magic)
        items = [PositionModel(**p.__dict__, time=p.time.isoformat()) for p in positions]
        return ApiResponse.success_response(
            data=items,
            metadata={
                "operation": "get_positions",
                "account_alias": active_alias,
                "symbol": symbol,
                "magic": magic,
                "count": len(items),
                "total_volume": sum(p.volume for p in positions),
                "total_profit": sum(p.profit for p in positions)
                if positions and hasattr(positions[0], "profit")
                else None,
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
    service: TradingModule = Depends(get_trading_service),
) -> ApiResponse[List[OrderModel]]:
    active_alias = service.active_account_alias
    try:
        orders = service.get_orders(symbol, magic)
        items = [OrderModel(**o.__dict__, time=o.time.isoformat()) for o in orders]
        return ApiResponse.success_response(
            data=items,
            metadata={
                "operation": "get_orders",
                "account_alias": active_alias,
                "symbol": symbol,
                "magic": magic,
                "count": len(items),
                "total_volume": sum(o.volume for o in orders),
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
def trading_accounts(service: TradingModule = Depends(get_trading_service)) -> ApiResponse[List[TradingAccountModel]]:
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


@router.get("/trade/operations", response_model=ApiResponse[List[dict]])
def trade_operations(
    operation_type: Optional[str] = Query(default=None, description="operation type"),
    status: Optional[str] = Query(default=None, description="operation status"),
    limit: int = Query(default=100, ge=1, le=500),
    service: TradingModule = Depends(get_trading_service),
) -> ApiResponse[List[dict]]:
    items = service.recent_operations(
        operation_type=operation_type,
        status=status,
        limit=limit,
    )
    return ApiResponse.success_response(
        data=items,
        metadata={
            "operation": "trade_operations",
            "account_alias": service.active_account_alias,
            "operation_type": operation_type,
            "status": status,
            "count": len(items),
        },
    )
