from __future__ import annotations

from typing import Any

from src.api.error_codes import AIErrorCode
from src.risk.service import resolve_risk_failure_key


_RISK_FAILURE_CODE_MAP: dict[str, AIErrorCode] = {
    "daily_loss_limit": AIErrorCode.DAILY_LOSS_LIMIT,
    "margin_availability": AIErrorCode.MARGIN_INSUFFICIENT_PRE,
    "trade_frequency": AIErrorCode.TRADE_FREQUENCY_LIMITED,
    "account_snapshot": AIErrorCode.POSITION_LIMIT_REACHED,
    "session_window": AIErrorCode.SESSION_WINDOW_BLOCKED,
}


def risk_error_code_from_assessment(assessment: dict[str, Any] | None) -> AIErrorCode:
    failure_key = resolve_risk_failure_key(assessment)
    return _RISK_FAILURE_CODE_MAP.get(
        failure_key,
        AIErrorCode.TRADE_BLOCKED_BY_RISK,
    )
