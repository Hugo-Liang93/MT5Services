from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import get_operator_command_service
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import (
    ApiResponse,
    BatchCancelOrdersRequest,
    BatchCloseRequest,
    CancelOrdersRequest,
    CloseAllRequest,
    CloseRequest,
    ExposureCloseoutRequest,
    TradeControlRequest,
)
from src.trading.application.idempotency import TradeOperatorActionReplayConflictError
from src.trading.commands.service import OperatorCommandService

from ..common import (
    build_idempotency_conflict_response,
    next_action_id,
    normalize_control_actor,
    normalize_idempotency_key,
    normalize_request_context,
)
from ..view_models import ExposureCloseoutActionView, TradeControlUpdateView, TradeMutationResultView

router = APIRouter(tags=["trade"])


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
