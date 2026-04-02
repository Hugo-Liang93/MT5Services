from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from src.api.deps import (
    get_position_manager,
    get_runtime_read_model,
    get_signal_service,
    get_trade_executor,
    get_trading_service,
)
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
    TradeControlRequest,
    TradeReconcileRequest,
    SignalExecuteTradeRequest,
    TradingAccountModel,
)
from src.clients.base import MT5TradeError
from src.risk.service import PreTradeRiskBlockedError
from src.readmodels.runtime import RuntimeReadModel
from src.signals.service import SignalModule
from src.trading.sizing import compute_trade_params, extract_atr_from_indicators
from src.trading.position_manager import PositionManager
from src.trading.signal_executor import TradeExecutor
from src.trading.service import TradingModule

router = APIRouter(tags=["trade"])


def _trade_request_details(request: TradeRequest) -> dict:
    return get_trade_error_details(
        symbol=request.symbol,
        volume=request.volume,
        side=request.side,
        price=request.price,
    )


def _position_model_from_dataclass(position) -> PositionModel:
    payload = dict(position.__dict__)
    payload["time"] = position.time.isoformat()
    return PositionModel(**payload)


def _order_model_from_dataclass(order) -> OrderModel:
    payload = dict(order.__dict__)
    payload["time"] = order.time.isoformat()
    return OrderModel(**payload)


def _risk_error_code_from_assessment(assessment: dict | None) -> AIErrorCode:
    checks = list((assessment or {}).get("checks") or [])
    if any(str(item.get("name")) == "daily_loss_limit" for item in checks):
        return AIErrorCode.DAILY_LOSS_LIMIT
    if str((assessment or {}).get("reason") or "").strip().lower() == "daily_loss_limit_reached":
        return AIErrorCode.DAILY_LOSS_LIMIT
    return AIErrorCode.TRADE_BLOCKED_BY_RISK


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


@router.get("/trade/control", response_model=ApiResponse[dict])
def trade_control_status(
    service: TradingModule = Depends(get_trading_service),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[dict]:
    return ApiResponse.success_response(
        data={
            "trade_control": service.trade_control_status(),
            "persisted_trade_control": runtime_views.persisted_trade_control_payload(),
            "trading_state": runtime_views.trading_state_summary(
                pending_limit=10,
                position_limit=10,
            ),
            "executor": runtime_views.trade_executor_summary(),
        },
        metadata={"operation": "trade_control_status"},
    )


@router.post("/trade/control", response_model=ApiResponse[dict])
def trade_control_update(
    request: TradeControlRequest,
    service: TradingModule = Depends(get_trading_service),
    executor: TradeExecutor = Depends(get_trade_executor),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[dict]:
    control = service.update_trade_control(
        auto_entry_enabled=request.auto_entry_enabled,
        close_only_mode=request.close_only_mode,
        reason=request.reason,
    )
    if request.reset_circuit:
        executor.reset_circuit()
    return ApiResponse.success_response(
        data={
            "trade_control": control,
            "executor": runtime_views.trade_executor_summary(),
        },
        metadata={
            "operation": "trade_control_update",
            "reset_circuit": request.reset_circuit,
        },
    )


@router.post("/trade/reconcile", response_model=ApiResponse[dict])
def trade_reconcile(
    request: TradeReconcileRequest,
    manager: PositionManager = Depends(get_position_manager),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[dict]:
    reconcile_result = (
        manager.sync_open_positions() if request.sync_open_positions else {"skipped": True}
    )
    return ApiResponse.success_response(
        data={
            "reconcile": reconcile_result,
            "position_manager": runtime_views.position_manager_summary(),
            "tracked_positions": runtime_views.tracked_positions_payload(limit=100),
            "trading_state": runtime_views.trading_state_summary(
                pending_limit=20,
                position_limit=20,
            ),
        },
        metadata={"operation": "trade_reconcile"},
    )


@router.get("/trade/state", response_model=ApiResponse[dict])
def trade_state_summary(
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[dict]:
    return ApiResponse.success_response(
        data=runtime_views.trading_state_summary(
            pending_limit=20,
            position_limit=20,
        ),
        metadata={"operation": "trade_state_summary"},
    )


@router.get("/trade/state/alerts", response_model=ApiResponse[dict])
def trade_state_alerts_summary(
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[dict]:
    return ApiResponse.success_response(
        data=runtime_views.trading_state_alerts_summary(),
        metadata={"operation": "trade_state_alerts_summary"},
    )


@router.get("/trade/state/pending/active", response_model=ApiResponse[dict])
def trade_active_pending_state_list(
    limit: int = Query(default=50, ge=1, le=500),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[dict]:
    return ApiResponse.success_response(
        data=runtime_views.active_pending_order_payload(limit=limit),
        metadata={
            "operation": "trade_active_pending_state_list",
            "limit": limit,
        },
    )


@router.get("/trade/state/execution-contexts", response_model=ApiResponse[dict])
def trade_pending_execution_context_list(
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[dict]:
    return ApiResponse.success_response(
        data=runtime_views.pending_execution_context_payload(),
        metadata={"operation": "trade_pending_execution_context_list"},
    )


@router.get("/trade/state/pending/lifecycle", response_model=ApiResponse[dict])
def trade_pending_lifecycle_state_list(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[dict]:
    normalized_status = str(status).strip() if isinstance(status, str) else None
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


@router.get("/trade/state/positions", response_model=ApiResponse[dict])
def trade_position_state_list(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[dict]:
    normalized_status = str(status).strip() if isinstance(status, str) else None
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
            error_code=_risk_error_code_from_assessment(exc.assessment),
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

    direction = signal_row.get("direction", "")
    if direction not in {"buy", "sell"}:
        return ApiResponse.error_response(
            error_code=AIErrorCode.VALIDATION_ERROR,
            error_message=f"Signal direction '{direction}' is not executable (buy/sell required)",
            suggested_action=AIErrorAction.VALIDATE_PARAMETERS,
            details={"signal_id": request.signal_id, "direction": direction},
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
        action=direction,
        current_price=entry_price,
        atr_value=atr,
        account_balance=balance,
    )
    volume = request.volume_override if request.volume_override is not None else params.position_size
    computed_params = {
        "symbol": signal_row.get("symbol"),
        "volume": volume,
        "side": direction,
        "order_kind": "market",
        "sl": params.stop_loss,
        "tp": params.take_profit,
        "comment": f"agent:{signal_row.get('strategy')}:{direction}:{request.signal_id[:8]}",
        "request_id": request.signal_id,
        "metadata": {
            "entry_origin": "auto",
            "regime": signal_row.get("metadata", {}).get("regime") if isinstance(signal_row.get("metadata"), dict) else None,
            "signal": {
                "signal_id": request.signal_id,
                "strategy": signal_row.get("strategy"),
                "timeframe": signal_row.get("timeframe"),
                "confidence": signal_row.get("confidence"),
                "direction": direction,
            },
        },
    }
    meta = {
        "operation": "trade_from_signal",
        "signal_id": request.signal_id,
        "direction": direction,
        "volume": volume,
        "sl": params.stop_loss,
        "tp": params.take_profit,
        "risk_reward_ratio": params.risk_reward_ratio,
        "account_alias": service.active_account_alias,
        "dry_run": request.dry_run,
    }
    if request.dry_run:
        return ApiResponse.success_response(data={"dry_run": True, **computed_params}, metadata=meta)
    result = service.dispatch_operation("trade", computed_params)
    return ApiResponse.success_response(
        data=result if isinstance(result, dict) else {"result": result},
        metadata=meta,
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
        items = [_position_model_from_dataclass(p) for p in positions]
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
        items = [_order_model_from_dataclass(o) for o in orders]
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
