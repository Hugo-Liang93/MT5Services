from __future__ import annotations

from typing import Any

from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.risk_adapter import risk_error_code_from_assessment
from src.api.schemas import ApiResponse, TradeRequest
from src.risk.service import PreTradeRiskBlockedError
from src.trading.admission import TradeAdmissionService
from src.trading.application import TradingCommandService

_OPERATION_ALIASES: dict[str, str] = {
    "trade": "trade",
    "submit_trade": "trade",
    "execute_trade": "trade",
    "trade_precheck": "trade_precheck",
    "precheck_trade": "trade_precheck",
}


class TradeAPIDispatcher:
    """统一交易 API 调度入口，减少路由层重复逻辑。"""

    def __init__(
        self,
        service: TradingCommandService,
        *,
        admission_service: TradeAdmissionService | None = None,
    ):
        self.service = service
        self._admission_service = admission_service

    @staticmethod
    def _normalize_dispatch_request(
        operation: str,
        payload: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any]]:
        requested_operation = str(operation or "").strip()
        canonical_operation = _OPERATION_ALIASES.get(
            requested_operation, requested_operation
        )
        normalized_payload = dict(payload or {})
        if canonical_operation in {"trade", "trade_precheck"}:
            normalized_payload = TradeRequest.model_validate(normalized_payload).model_dump(
                exclude_none=True
            )
            normalized_payload.pop("direction", None)
        return canonical_operation, normalized_payload

    def dispatch(self, operation: str, payload: dict[str, Any] | None = None) -> ApiResponse[Any]:
        requested_operation = str(operation or "").strip()
        try:
            normalized_operation, normalized_payload = self._normalize_dispatch_request(
                requested_operation,
                payload,
            )
            admission_evaluation: dict[str, Any] | None = None
            admission_report: dict[str, Any] | None = None
            if (
                self._admission_service is not None
                and normalized_operation in {"trade", "trade_precheck"}
            ):
                admission_evaluation = self._admission_service.evaluate_trade_payload(
                    normalized_payload,
                    requested_operation=requested_operation,
                )
                admission_report = dict(admission_evaluation.get("report") or {})
                if normalized_operation == "trade_precheck":
                    return ApiResponse.success_response(
                        data=admission_report,
                        metadata={
                            "operation": normalized_operation,
                            "requested_operation": requested_operation,
                            "account_alias": self.service.active_account_alias,
                            "trace_id": admission_report.get("trace_id"),
                        },
                    )
                if admission_report and str(admission_report.get("decision") or "").lower() == "block":
                    assessment = dict(admission_evaluation.get("assessment") or {})
                    reasons = list(admission_report.get("reasons") or [])
                    primary_reason = reasons[0] if reasons else {}
                    return ApiResponse.error_response(
                        error_code=risk_error_code_from_assessment(assessment),
                        error_message=str(
                            assessment.get("reason")
                            or primary_reason.get("message")
                            or "trade blocked by admission"
                        ),
                        suggested_action=AIErrorAction.WAIT_FOR_RISK_WINDOW,
                        details={
                            "operation": requested_operation,
                            "account_alias": self.service.active_account_alias,
                            "risk_assessment": assessment,
                            "admission_report": admission_report,
                        },
                    )
            result = self.service.dispatch_operation(
                normalized_operation,
                normalized_payload,
            )
            return ApiResponse.success_response(
                data=(
                    {
                        "result": result,
                        "admission_report": admission_report,
                    }
                    if admission_report is not None
                    else result
                ),
                metadata={
                    "operation": normalized_operation,
                    "requested_operation": requested_operation,
                    "account_alias": self.service.active_account_alias,
                    "trace_id": admission_report.get("trace_id") if admission_report else None,
                },
            )
        except ValueError as exc:
            return ApiResponse.error_response(
                error_code=AIErrorCode.INVALID_REQUEST,
                error_message=str(exc),
                suggested_action=AIErrorAction.REVIEW_PARAMETERS,
                details={"operation": requested_operation},
            )
        except PreTradeRiskBlockedError as exc:
            return ApiResponse.error_response(
                error_code=risk_error_code_from_assessment(exc.assessment),
                error_message=str(exc),
                suggested_action=AIErrorAction.WAIT_FOR_RISK_WINDOW,
                details={
                    "operation": requested_operation,
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
                    "operation": requested_operation,
                    "account_alias": self.service.active_account_alias,
                    "exception_type": type(exc).__name__,
                },
            )
