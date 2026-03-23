from __future__ import annotations

from typing import Any

from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse
from src.risk.service import PreTradeRiskBlockedError
from src.trading.service import TradingModule


_RISK_RULE_ERROR_MAP: dict[str, AIErrorCode] = {
    "daily_loss_limit": AIErrorCode.DAILY_LOSS_LIMIT,
    "margin_availability": AIErrorCode.MARGIN_INSUFFICIENT_PRE,
    "trade_frequency": AIErrorCode.TRADE_FREQUENCY_LIMITED,
    "account_snapshot": AIErrorCode.POSITION_LIMIT_REACHED,
    "session_window": AIErrorCode.SESSION_WINDOW_BLOCKED,
}


def _risk_error_code(assessment: dict[str, Any] | None) -> AIErrorCode:
    checks = list((assessment or {}).get("checks") or [])
    # Match the first failed rule to a specific error code
    for item in checks:
        rule_name = str(item.get("name") or "").strip()
        if rule_name in _RISK_RULE_ERROR_MAP:
            return _RISK_RULE_ERROR_MAP[rule_name]
    reason = str((assessment or {}).get("reason") or "").strip().lower()
    if "daily_loss_limit" in reason:
        return AIErrorCode.DAILY_LOSS_LIMIT
    return AIErrorCode.TRADE_BLOCKED_BY_RISK


class TradeAPIDispatcher:
    """统一交易 API 调度入口，减少路由层重复逻辑。"""

    def __init__(self, service: TradingModule):
        self.service = service

    def dispatch(self, operation: str, payload: dict[str, Any] | None = None) -> ApiResponse[Any]:
        try:
            result = self.service.dispatch_operation(operation, payload or {})
            return ApiResponse.success_response(
                data=result,
                metadata={
                    "operation": operation,
                    "account_alias": self.service.active_account_alias,
                },
            )
        except ValueError as exc:
            return ApiResponse.error_response(
                error_code=AIErrorCode.INVALID_REQUEST,
                error_message=str(exc),
                suggested_action=AIErrorAction.REVIEW_PARAMETERS,
                details={"operation": operation},
            )
        except PreTradeRiskBlockedError as exc:
            return ApiResponse.error_response(
                error_code=_risk_error_code(exc.assessment),
                error_message=str(exc),
                suggested_action=AIErrorAction.WAIT_FOR_RISK_WINDOW,
                details={
                    "operation": operation,
                    "account_alias": self.service.active_account_alias,
                    "risk_assessment": exc.assessment,
                },
            )
        except Exception as exc:
            return ApiResponse.error_response(
                error_code=AIErrorCode.UNKNOWN_ERROR,
                error_message=f"dispatch failed: {exc}",
                suggested_action=AIErrorAction.RETRY_AFTER_DELAY,
                details={
                    "operation": operation,
                    "account_alias": self.service.active_account_alias,
                    "exception_type": type(exc).__name__,
                },
            )
