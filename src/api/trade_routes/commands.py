from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import (
    get_exposure_closeout_controller,
    get_trade_admission_service,
    get_operator_command_service,
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
    AdmissionReportModel,
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
    TradeOperatorActionReplayConflictError,
    TradingCommandService,
    TradingQueryService,
)
from src.trading.closeout import ExposureCloseoutController
from src.trading.admission import TradeAdmissionService
from src.trading.commands import OperatorCommandService
from src.trading.execution import TradeExecutor
from src.trading.positions import PositionManager

from .common import (
    build_control_action_error_details,
    build_control_action_error_payload,
    build_control_action_result,
    build_idempotency_conflict_response,
    build_replayed_action_response,
    next_action_id,
    normalize_control_actor,
    normalize_idempotency_key,
    normalize_request_context,
    trade_request_details,
)
from .view_models import ExposureCloseoutActionView, TradeControlUpdateView
from .view_models import TradeMutationResultView

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


def _operator_action_replay_response(
    *,
    service: TradingCommandService,
    command_type: str,
    operation: str,
    idempotency_key: Optional[str],
    request_payload: dict[str, object],
    extra_metadata: Optional[dict[str, object]] = None,
    default_error_code: object = None,
    default_suggested_action: object = None,
) -> Optional[ApiResponse[dict]]:
    if not idempotency_key:
        return None
    try:
        replayed = service.find_operator_action_replay(
            command_type=command_type,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )
    except TradeOperatorActionReplayConflictError as exc:
        return build_idempotency_conflict_response(
            operation=operation,
            command_type=command_type,
            idempotency_key=idempotency_key,
            existing_record=exc.existing_record,
            extra_metadata=extra_metadata,
        )
    if replayed is None:
        return None
    return build_replayed_action_response(
        operation=operation,
        replayed=replayed,
        extra_metadata=extra_metadata,
        default_error_code=default_error_code,
        default_suggested_action=default_suggested_action,
    )


def _mutation_result_details(result: dict[str, object]) -> dict[str, object]:
    details = dict(result.get("details") or {}) if isinstance(result.get("details"), dict) else {}
    for key in (
        "status",
        "action_id",
        "audit_id",
        "actor",
        "reason",
        "idempotency_key",
        "request_context",
        "recorded_at",
    ):
        value = result.get(key)
        if value is not None:
            details.setdefault(key, value)
    return details


def _mutation_result_response(
    *,
    result: dict[str, object],
    operation: str,
    success_metadata: dict[str, object],
    error_code: object,
    suggested_action: object,
) -> ApiResponse[dict]:
    if bool(result.get("accepted", True)):
        return ApiResponse.success_response(data=result, metadata=success_metadata)
    return ApiResponse.error_response(
        error_code=error_code,
        error_message=str(
            result.get("error_message")
            or result.get("message")
            or f"{operation} failed"
        ),
        suggested_action=suggested_action,
        details=_mutation_result_details(result),
    )


def _queue_operator_command_response(
    *,
    service: OperatorCommandService,
    command_type: str,
    operation: str,
    payload: dict[str, object],
    actor: str,
    reason: str | None,
    action_id: str,
    idempotency_key: str | None,
    request_context: dict[str, object],
    target_account_alias: str | None,
    metadata: dict[str, object] | None = None,
    error_code: object = AIErrorCode.UNKNOWN_ERROR,
    suggested_action: object = AIErrorAction.CONTACT_SUPPORT,
) -> ApiResponse[dict]:
    try:
        result = service.enqueue(
            command_type=command_type,
            payload=payload,
            actor=actor,
            reason=reason,
            action_id=action_id,
            idempotency_key=idempotency_key,
            request_context=request_context,
            target_account_alias=target_account_alias,
        )
    except TradeOperatorActionReplayConflictError as exc:
        return build_idempotency_conflict_response(
            operation=operation,
            command_type=command_type,
            idempotency_key=idempotency_key,
            existing_record=exc.existing_record,
            extra_metadata=metadata,
        )
    except Exception as exc:
        return ApiResponse.error_response(
            error_code=error_code,
            error_message=str(exc),
            suggested_action=suggested_action,
            details={
                "operation": operation,
                "command_type": command_type,
                "action_id": action_id,
                "actor": actor,
                "reason": reason,
                "idempotency_key": idempotency_key,
                "request_context": request_context,
                "target_account_alias": target_account_alias,
                "payload": payload,
            },
        )
    success_metadata = {
        "operation": operation,
        "action_id": result.get("action_id") or action_id,
        "command_id": result.get("command_id"),
        "audit_id": result.get("audit_id"),
        "actor": actor,
        "target_account_alias": (
            result.get("effective_state", {}) or {}
        ).get("target_account_alias")
        or target_account_alias,
    }
    if metadata:
        success_metadata.update(metadata)
    if bool(result.get("replayed")):
        success_metadata["replayed"] = True
    if bool(result.get("accepted", True)):
        return ApiResponse.success_response(data=result, metadata=success_metadata)
    return ApiResponse.error_response(
        error_code=error_code,
        error_message=str(result.get("error_message") or result.get("message") or operation),
        suggested_action=suggested_action,
        details=_mutation_result_details(result),
    )


@router.post("/trade/dispatch", response_model=ApiResponse[dict])
def trade_dispatch(
    request: TradeDispatchRequest,
    service: TradingCommandService = Depends(get_trading_command_service),
    admission_service: TradeAdmissionService = Depends(get_trade_admission_service),
) -> ApiResponse[dict]:
    dispatcher = TradeAPIDispatcher(service, admission_service=admission_service)
    return dispatcher.dispatch(request.operation, request.payload)


@router.post("/trade/control", response_model=ApiResponse[TradeControlUpdateView])
def trade_control_update(
    request: TradeControlRequest,
    command_queue: OperatorCommandService = Depends(get_operator_command_service),
) -> ApiResponse[TradeControlUpdateView]:
    command_type = "set_trade_control"
    actor = normalize_control_actor(request.actor)
    idempotency_key = normalize_idempotency_key(request.idempotency_key)
    reason = str(request.reason or "").strip() or None
    request_context = normalize_request_context(request.request_context)
    action_id = next_action_id()
    request_payload = {
        "auto_entry_enabled": request.auto_entry_enabled,
        "close_only_mode": request.close_only_mode,
        "reset_circuit": request.reset_circuit,
        "scope": "trade_control",
    }
    return _queue_operator_command_response(
        service=command_queue,
        command_type=command_type,
        operation="trade_control_update",
        payload=request_payload,
        actor=actor,
        reason=reason,
        action_id=action_id,
        idempotency_key=idempotency_key,
        request_context=request_context,
        target_account_alias=request.account_alias,
        metadata={"reset_circuit": request.reset_circuit},
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


@router.post("/trade/precheck", response_model=ApiResponse[AdmissionReportModel])
def trade_precheck(
    request: TradeRequest,
    service: TradingCommandService = Depends(get_trading_command_service),
    admission_service: TradeAdmissionService = Depends(get_trade_admission_service),
) -> ApiResponse[AdmissionReportModel]:
    active_alias = service.active_account_alias
    try:
        evaluation = admission_service.evaluate_trade_payload(
            request.model_dump(exclude_none=True),
            requested_operation="trade_precheck",
        )
        report = dict(evaluation.get("report") or {})
        return ApiResponse.success_response(
            data=AdmissionReportModel(**report),
            metadata={
                "operation": "trade_precheck",
                "account_alias": active_alias,
                "symbol": request.symbol,
                "side": request.side,
                "volume": request.volume,
                "trace_id": report.get("trace_id"),
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


@router.post("/close", response_model=ApiResponse[TradeMutationResultView])
def close(
    request: CloseRequest,
    command_queue: OperatorCommandService = Depends(get_operator_command_service),
) -> ApiResponse[TradeMutationResultView]:
    command_type = "close_position"
    actor = normalize_control_actor(request.actor)
    idempotency_key = normalize_idempotency_key(request.idempotency_key)
    reason = str(request.reason or "").strip() or "manual_close"
    request_context = normalize_request_context(request.request_context)
    action_id = next_action_id()
    request_payload = {
        "ticket": request.ticket,
        "volume": request.volume,
        "deviation": request.deviation,
        "comment": request.comment,
        "scope": "position",
    }
    return _queue_operator_command_response(
        service=command_queue,
        command_type=command_type,
        operation="position_close",
        payload=request_payload,
        actor=actor,
        reason=reason,
        action_id=action_id,
        idempotency_key=idempotency_key,
        request_context=request_context,
        target_account_alias=request.account_alias,
        metadata={"ticket": request.ticket},
        error_code=AIErrorCode.TRADE_CLOSE_FAILED,
        suggested_action=AIErrorAction.CLOSE_POSITION,
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


@router.post("/close_all", response_model=ApiResponse[TradeMutationResultView])
def close_all(
    request: CloseAllRequest,
    command_queue: OperatorCommandService = Depends(get_operator_command_service),
) -> ApiResponse[TradeMutationResultView]:
    command_type = "close_all_positions"
    actor = normalize_control_actor(request.actor)
    idempotency_key = normalize_idempotency_key(request.idempotency_key)
    reason = str(request.reason or "").strip() or "manual_close_all"
    request_context = normalize_request_context(request.request_context)
    action_id = next_action_id()
    request_payload = {
        "symbol": request.symbol,
        "magic": request.magic,
        "side": request.side,
        "deviation": request.deviation,
        "comment": request.comment,
        "scope": "positions",
    }
    return _queue_operator_command_response(
        service=command_queue,
        command_type=command_type,
        operation="close_all_positions",
        payload=request_payload,
        actor=actor,
        reason=reason,
        action_id=action_id,
        idempotency_key=idempotency_key,
        request_context=request_context,
        target_account_alias=request.account_alias,
        metadata={
            "symbol": request.symbol,
            "magic": request.magic,
            "side": request.side,
        },
        error_code=AIErrorCode.TRADE_CLOSE_FAILED,
        suggested_action=AIErrorAction.CLOSE_POSITION,
    )


@router.post("/trade/closeout-exposure", response_model=ApiResponse[ExposureCloseoutActionView])
def trade_closeout_exposure(
    request: ExposureCloseoutRequest,
    command_queue: OperatorCommandService = Depends(get_operator_command_service),
) -> ApiResponse[ExposureCloseoutActionView]:
    command_type = "close_exposure"
    actor = normalize_control_actor(request.actor)
    idempotency_key = normalize_idempotency_key(request.idempotency_key)
    reason = str(request.reason or "").strip() or "manual_risk_off"
    request_context = normalize_request_context(request.request_context)
    action_id = next_action_id()
    request_payload = {
        "comment": request.comment,
        "scope": "exposure",
    }
    return _queue_operator_command_response(
        service=command_queue,
        command_type=command_type,
        operation="trade_closeout_exposure",
        payload=request_payload,
        actor=actor,
        reason=reason,
        action_id=action_id,
        idempotency_key=idempotency_key,
        request_context=request_context,
        target_account_alias=request.account_alias,
        metadata={"comment": request.comment},
    )


@router.post("/close/batch", response_model=ApiResponse[TradeMutationResultView])
def close_batch(
    request: BatchCloseRequest,
    command_queue: OperatorCommandService = Depends(get_operator_command_service),
) -> ApiResponse[TradeMutationResultView]:
    command_type = "close_positions_batch"
    actor = normalize_control_actor(request.actor)
    idempotency_key = normalize_idempotency_key(request.idempotency_key)
    reason = str(request.reason or "").strip() or "manual_close_batch"
    request_context = normalize_request_context(request.request_context)
    action_id = next_action_id()
    request_payload = {
        "tickets": list(request.tickets),
        "deviation": request.deviation,
        "comment": request.comment,
        "scope": "positions",
    }
    return _queue_operator_command_response(
        service=command_queue,
        command_type=command_type,
        operation="close_batch",
        payload=request_payload,
        actor=actor,
        reason=reason,
        action_id=action_id,
        idempotency_key=idempotency_key,
        request_context=request_context,
        target_account_alias=request.account_alias,
        metadata={"count": len(request.tickets)},
        error_code=AIErrorCode.TRADE_CLOSE_FAILED,
        suggested_action=AIErrorAction.CLOSE_POSITION,
    )


@router.post("/cancel_orders", response_model=ApiResponse[TradeMutationResultView])
def cancel_orders(
    request: CancelOrdersRequest,
    command_queue: OperatorCommandService = Depends(get_operator_command_service),
) -> ApiResponse[TradeMutationResultView]:
    command_type = "cancel_orders"
    actor = normalize_control_actor(request.actor)
    idempotency_key = normalize_idempotency_key(request.idempotency_key)
    reason = str(request.reason or "").strip() or "manual_cancel_orders"
    request_context = normalize_request_context(request.request_context)
    action_id = next_action_id()
    request_payload = {
        "symbol": request.symbol,
        "magic": request.magic,
        "scope": "orders",
    }
    return _queue_operator_command_response(
        service=command_queue,
        command_type=command_type,
        operation="cancel_orders",
        payload=request_payload,
        actor=actor,
        reason=reason,
        action_id=action_id,
        idempotency_key=idempotency_key,
        request_context=request_context,
        target_account_alias=request.account_alias,
        metadata={"symbol": request.symbol, "magic": request.magic},
        error_code=AIErrorCode.TRADE_CANCEL_FAILED,
        suggested_action=AIErrorAction.CANCEL_ORDER,
    )


@router.post("/cancel_orders/batch", response_model=ApiResponse[TradeMutationResultView])
def cancel_orders_batch(
    request: BatchCancelOrdersRequest,
    command_queue: OperatorCommandService = Depends(get_operator_command_service),
) -> ApiResponse[TradeMutationResultView]:
    command_type = "cancel_orders_batch"
    actor = normalize_control_actor(request.actor)
    idempotency_key = normalize_idempotency_key(request.idempotency_key)
    reason = str(request.reason or "").strip() or "manual_cancel_orders_batch"
    request_context = normalize_request_context(request.request_context)
    action_id = next_action_id()
    request_payload = {
        "tickets": list(request.tickets),
        "scope": "orders",
    }
    return _queue_operator_command_response(
        service=command_queue,
        command_type=command_type,
        operation="cancel_orders_batch",
        payload=request_payload,
        actor=actor,
        reason=reason,
        action_id=action_id,
        idempotency_key=idempotency_key,
        request_context=request_context,
        target_account_alias=request.account_alias,
        metadata={"count": len(request.tickets)},
        error_code=AIErrorCode.TRADE_CANCEL_FAILED,
        suggested_action=AIErrorAction.CANCEL_ORDER,
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
