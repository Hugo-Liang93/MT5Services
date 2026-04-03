from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import (
    get_exposure_closeout_controller,
    get_position_manager,
    get_runtime_read_model,
    get_signal_service,
    get_trade_executor,
    get_trading_command_service,
    get_trading_query_service,
)
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.risk_adapter import risk_error_code_from_assessment
from src.api.schemas import (
    ApiResponse,
    BatchCancelOrdersRequest,
    BatchCloseRequest,
    BatchTradeRequest,
    CancelOrdersRequest,
    CloseAllRequest,
    CloseRequest,
    EstimateMarginRequest,
    ExposureCloseoutRequest,
    ModifyOrdersRequest,
    ModifyPositionsRequest,
    SignalExecuteTradeRequest,
    TradeDispatchRequest,
    TradePrecheckModel,
    TradeReconcileRequest,
    TradeRequest,
    TradeControlRequest,
)
from src.api.trade_dispatcher import TradeAPIDispatcher
from src.clients.base import MT5TradeError
from src.readmodels.runtime import RuntimeReadModel
from src.risk.service import PreTradeRiskBlockedError
from src.signals.service import SignalModule
from src.trading.application import (
    SignalTradeCommandService,
    SignalTradePreparationError,
    TradingCommandService,
    TradingQueryService,
)
from src.trading.closeout import ExposureCloseoutController
from src.trading.execution import TradeExecutor
from src.trading.positions import PositionManager

from .common import trade_request_details
from .view_models import TradeControlUpdateView

router = APIRouter(tags=["trade"])


def _signal_trade_service(
    *,
    signal_service: SignalModule,
    command_service: TradingCommandService,
    query_service: TradingQueryService,
) -> SignalTradeCommandService:
    return SignalTradeCommandService(
        signal_service=signal_service,
        command_service=command_service,
        query_service=query_service,
    )


@router.post("/trade/dispatch", response_model=ApiResponse[dict])
def trade_dispatch(
    request: TradeDispatchRequest,
    service: TradingCommandService = Depends(get_trading_command_service),
) -> ApiResponse[dict]:
    dispatcher = TradeAPIDispatcher(service)
    return dispatcher.dispatch(request.operation, request.payload)


@router.post("/trade/control", response_model=ApiResponse[TradeControlUpdateView])
def trade_control_update(
    request: TradeControlRequest,
    service: TradingCommandService = Depends(get_trading_command_service),
    executor: TradeExecutor = Depends(get_trade_executor),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[TradeControlUpdateView]:
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


@router.post("/trade/precheck", response_model=ApiResponse[TradePrecheckModel])
def trade_precheck(
    request: TradeRequest,
    service: TradingCommandService = Depends(get_trading_command_service),
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
                **trade_request_details(request),
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
                **trade_request_details(request),
            },
        )


@router.post("/trade", response_model=ApiResponse[dict])
def trade(
    request: TradeRequest,
    service: TradingCommandService = Depends(get_trading_command_service),
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
                details=trade_request_details(request),
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
            error_code=risk_error_code_from_assessment(exc.assessment),
            error_message=str(exc),
            suggested_action=AIErrorAction.WAIT_FOR_RISK_WINDOW,
            details={
                **trade_request_details(request),
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
                **trade_request_details(request),
            },
        )


@router.post("/trade/from-signal", response_model=ApiResponse[dict])
def trade_from_signal(
    request: SignalExecuteTradeRequest,
    signal_service: SignalModule = Depends(get_signal_service),
    command_service: TradingCommandService = Depends(get_trading_command_service),
    query_service: TradingQueryService = Depends(get_trading_query_service),
) -> ApiResponse[dict]:
    service = _signal_trade_service(
        signal_service=signal_service,
        command_service=command_service,
        query_service=query_service,
    )
    try:
        prepared = service.prepare_trade(
            signal_id=request.signal_id,
            volume_override=request.volume_override,
        )
    except SignalTradePreparationError as exc:
        if exc.category == "not_found":
            error_code = AIErrorCode.DATA_NOT_AVAILABLE
            suggested_action = AIErrorAction.USE_FALLBACK_DATA
        elif exc.category == "validation":
            error_code = AIErrorCode.VALIDATION_ERROR
            suggested_action = AIErrorAction.VALIDATE_PARAMETERS
        elif exc.category == "account":
            error_code = AIErrorCode.ACCOUNT_INFO_FAILED
            suggested_action = AIErrorAction.CHECK_ACCOUNT_STATUS
        else:
            error_code = AIErrorCode.DATA_NOT_AVAILABLE
            suggested_action = AIErrorAction.WAIT_FOR_DATA
        return ApiResponse.error_response(
            error_code=error_code,
            error_message=str(exc),
            suggested_action=suggested_action,
            details=exc.details,
        )

    metadata = {
        **prepared.execution_metadata,
        "dry_run": request.dry_run,
    }
    if request.dry_run:
        return ApiResponse.success_response(
            data={"dry_run": True, **prepared.trade_request},
            metadata=metadata,
        )
    result = service.execute_prepared(prepared)
    return ApiResponse.success_response(
        data=result if isinstance(result, dict) else {"result": result},
        metadata=metadata,
    )


@router.post("/close", response_model=ApiResponse[dict])
def close(
    request: CloseRequest,
    service: TradingCommandService = Depends(get_trading_command_service),
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
    service: TradingCommandService = Depends(get_trading_command_service),
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
    service: TradingCommandService = Depends(get_trading_command_service),
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


@router.post("/trade/closeout-exposure", response_model=ApiResponse[dict])
def trade_closeout_exposure(
    request: ExposureCloseoutRequest,
    controller: ExposureCloseoutController = Depends(get_exposure_closeout_controller),
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[dict]:
    result = controller.execute(
        reason=request.reason,
        comment=request.comment,
    )
    return ApiResponse.success_response(
        data={
            "closeout": result,
            "trading_state": runtime_views.trading_state_summary(
                pending_limit=20,
                position_limit=20,
            ),
        },
        metadata={
            "operation": "trade_closeout_exposure",
            "reason": request.reason,
            "comment": request.comment,
        },
    )


@router.post("/close/batch", response_model=ApiResponse[dict])
def close_batch(
    request: BatchCloseRequest,
    service: TradingCommandService = Depends(get_trading_command_service),
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
    service: TradingCommandService = Depends(get_trading_command_service),
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
    service: TradingCommandService = Depends(get_trading_command_service),
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
    service: TradingCommandService = Depends(get_trading_command_service),
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
                **trade_request_details(
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
    service: TradingCommandService = Depends(get_trading_command_service),
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
    service: TradingCommandService = Depends(get_trading_command_service),
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
