from __future__ import annotations

from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.risk_adapter import risk_error_code_from_assessment
from src.api.schemas import ApiResponse
from src.clients.base import MT5TradeError
from src.risk.service import PreTradeRiskBlockedError
from src.signals.service import SignalModule
from src.trading.application.services import TradingCommandService, TradingQueryService
from src.trading.application.signal_execution import SignalTradeCommandService


def build_signal_trade_service(
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


def trade_request_payload_details(payload: dict[str, object]) -> dict[str, object]:
    details: dict[str, object] = {}
    for key in ("symbol", "volume", "side", "price", "order_kind", "request_id"):
        value = payload.get(key)
        if value is not None:
            details[key] = value
    return details


def build_trade_risk_blocked_response(
    *,
    exc: PreTradeRiskBlockedError,
    details: dict[str, object],
    account_alias: str,
) -> ApiResponse[dict]:
    return ApiResponse.error_response(
        error_code=risk_error_code_from_assessment(exc.assessment),
        error_message=str(exc),
        suggested_action=AIErrorAction.WAIT_FOR_RISK_WINDOW,
        details={
            **details,
            "risk_assessment": exc.assessment,
            "account_alias": account_alias,
        },
    )


def build_trade_execution_error_response(
    *,
    exc: MT5TradeError,
    details: dict[str, object],
    account_alias: str,
    default_error_code: object = AIErrorCode.TRADE_EXECUTION_FAILED,
    default_suggested_action: object = AIErrorAction.RETRY_AFTER_DELAY,
    error_prefix: str = "Trade error",
) -> ApiResponse[dict]:
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
    elif "position_not_found" in error_msg.lower() or "position not found" in error_msg.lower():
        error_code = AIErrorCode.POSITION_NOT_FOUND
        suggested_action = AIErrorAction.CHECK_ACCOUNT_STATUS
    elif "order_not_found" in error_msg.lower() or "order not found" in error_msg.lower():
        error_code = AIErrorCode.ORDER_NOT_FOUND
        suggested_action = AIErrorAction.CHECK_ACCOUNT_STATUS
    else:
        error_code = default_error_code
        suggested_action = default_suggested_action
    return ApiResponse.error_response(
        error_code=error_code,
        error_message=f"{error_prefix}: {error_msg}",
        suggested_action=suggested_action,
        details={
            "exception_type": type(exc).__name__,
            "account_alias": account_alias,
            **details,
        },
    )


def build_unexpected_trade_operation_response(
    *,
    operation: str,
    exc: Exception,
    details: dict[str, object],
    account_alias: str,
    suggested_action: object = AIErrorAction.CONTACT_SUPPORT,
) -> ApiResponse[dict]:
    return ApiResponse.error_response(
        error_code=AIErrorCode.UNKNOWN_ERROR,
        error_message=f"{operation} failed: {str(exc)}",
        suggested_action=suggested_action,
        details={
            "exception_type": type(exc).__name__,
            "account_alias": account_alias,
            **details,
        },
    )
