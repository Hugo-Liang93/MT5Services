from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import (
    get_signal_service,
    get_trading_command_service,
    get_trading_query_service,
)
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse, SignalExecuteTradeRequest
from src.clients.base import MT5TradeError
from src.risk.service import PreTradeRiskBlockedError
from src.signals.service import SignalModule
from src.trading.application.services import TradingCommandService, TradingQueryService
from src.trading.application.signal_execution import SignalTradePreparationError

from .common import (
    build_signal_trade_service,
    build_trade_execution_error_response,
    build_trade_risk_blocked_response,
    build_unexpected_trade_operation_response,
    trade_request_payload_details,
)

router = APIRouter(tags=["trade"])


@router.post("/trade/from-signal", response_model=ApiResponse[dict])
def trade_from_signal(
    request: SignalExecuteTradeRequest,
    signal_service: SignalModule = Depends(get_signal_service),
    command_service: TradingCommandService = Depends(get_trading_command_service),
    query_service: TradingQueryService = Depends(get_trading_query_service),
) -> ApiResponse[dict]:
    service = build_signal_trade_service(
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
    try:
        result = service.execute_prepared(prepared)
        return ApiResponse.success_response(
            data=result if isinstance(result, dict) else {"result": result},
            metadata=metadata,
        )
    except PreTradeRiskBlockedError as exc:
        return build_trade_risk_blocked_response(
            exc=exc,
            details=trade_request_payload_details(prepared.trade_request),
            account_alias=command_service.active_account_alias,
        )
    except MT5TradeError as exc:
        return build_trade_execution_error_response(
            exc=exc,
            details=trade_request_payload_details(prepared.trade_request),
            account_alias=command_service.active_account_alias,
        )
    except Exception as exc:
        return build_unexpected_trade_operation_response(
            operation="trade_from_signal",
            exc=exc,
            details={
                **trade_request_payload_details(prepared.trade_request),
                "signal_id": request.signal_id,
            },
            account_alias=command_service.active_account_alias,
        )
