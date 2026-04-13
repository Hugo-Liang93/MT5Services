from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import (
    get_operator_command_service,
    get_runtime_mode_controller,
    get_runtime_read_model,
    get_trading_command_service,
)
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse, RuntimeModeRequest
from src.app_runtime.mode_controller import RuntimeModeController
from src.readmodels.runtime import RuntimeReadModel
from src.trading.application import (
    TradeOperatorActionReplayConflictError,
    TradingCommandService,
)
from src.trading.commands import OperatorCommandService

from .common import (
    build_control_action_error_payload,
    build_control_action_result,
    build_idempotency_conflict_response,
    build_replayed_action_response,
    next_action_id,
    normalize_control_actor,
    normalize_idempotency_key,
    normalize_request_context,
)
from .view_models import RuntimeModeSummaryView, RuntimeModeUpdateView

router = APIRouter(tags=["trade"])


@router.get("/trade/runtime-mode", response_model=ApiResponse[RuntimeModeSummaryView])
def trade_runtime_mode_status(
    runtime_views: RuntimeReadModel = Depends(get_runtime_read_model),
) -> ApiResponse[RuntimeModeSummaryView]:
    return ApiResponse.success_response(
        data=runtime_views.runtime_mode_summary(),
        metadata={"operation": "trade_runtime_mode_status"},
    )


@router.post("/trade/runtime-mode", response_model=ApiResponse[RuntimeModeUpdateView])
def trade_runtime_mode_update(
    request: RuntimeModeRequest,
    command_queue: OperatorCommandService = Depends(get_operator_command_service),
) -> ApiResponse[RuntimeModeUpdateView]:
    command_type = "set_runtime_mode"
    actor = normalize_control_actor(request.actor)
    idempotency_key = normalize_idempotency_key(request.idempotency_key)
    reason = str(request.reason or "").strip() or "api"
    request_context = normalize_request_context(request.request_context)
    action_id = next_action_id()
    request_payload = {
        "mode": request.mode,
        "scope": "runtime_mode",
    }
    try:
        result = command_queue.enqueue(
            command_type=command_type,
            payload=request_payload,
            actor=actor,
            reason=reason,
            action_id=action_id,
            idempotency_key=idempotency_key,
            request_context=request_context,
            target_account_alias=request.account_alias,
        )
    except TradeOperatorActionReplayConflictError as exc:
        return build_idempotency_conflict_response(
            operation="trade_runtime_mode_update",
            command_type=command_type,
            idempotency_key=idempotency_key,
            existing_record=exc.existing_record,
            extra_metadata={"mode": request.mode},
        )
    except Exception as exc:
        return ApiResponse.error_response(
            error_code=AIErrorCode.INVALID_PARAMETER_VALUE,
            error_message=str(exc),
            suggested_action=AIErrorAction.VALIDATE_PARAMETERS,
            details={
                "operation": "trade_runtime_mode_update",
                "mode": request.mode,
                "target_account_alias": request.account_alias,
                "action_id": action_id,
            },
        )
    if bool(result.get("accepted", True)):
        metadata = {
            "operation": "trade_runtime_mode_update",
            "action_id": result.get("action_id") or action_id,
            "command_id": result.get("command_id"),
            "audit_id": result.get("audit_id"),
            "actor": actor,
            "target_account_alias": (
                result.get("effective_state", {}) or {}
            ).get("target_account_alias")
            or request.account_alias,
        }
        if bool(result.get("replayed")):
            metadata["replayed"] = True
        return ApiResponse.success_response(data=result, metadata=metadata)
    return ApiResponse.error_response(
        error_code=AIErrorCode.INVALID_PARAMETER_VALUE,
        error_message=str(
            result.get("error_message") or result.get("message") or "runtime mode update failed"
        ),
        suggested_action=AIErrorAction.VALIDATE_PARAMETERS,
        details=result,
    )
