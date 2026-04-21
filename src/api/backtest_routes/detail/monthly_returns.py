"""GET /v1/backtest/history/{run_id}/monthly-returns — 月度收益（热力图，P11 Phase 1）。"""

from __future__ import annotations

import logging
from typing import cast

from fastapi import APIRouter, Depends

from src.api import deps
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse
from src.readmodels.backtest_detail import BacktestDetailReadModel

from .schemas import BacktestMonthlyReturns

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/history/{run_id}/monthly-returns",
    response_model=ApiResponse[BacktestMonthlyReturns],
)
def get_monthly_returns(
    run_id: str,
    read_model: BacktestDetailReadModel = Depends(deps.get_backtest_detail_read_model),
) -> ApiResponse[BacktestMonthlyReturns]:
    try:
        payload = read_model.build_monthly_returns(run_id)
    except Exception as exc:
        logger.exception("monthly-returns build failed for %s", run_id)
        return cast(
            ApiResponse[BacktestMonthlyReturns],
            ApiResponse.error_response(
                error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
                error_message=str(exc),
                suggested_action=AIErrorAction.CONTACT_SUPPORT,
            ),
        )
    if payload is None:
        return cast(
            ApiResponse[BacktestMonthlyReturns],
            ApiResponse.error_response(
                error_code=AIErrorCode.NOT_FOUND,
                error_message=f"Backtest run not found: {run_id}",
                suggested_action=AIErrorAction.CONTACT_SUPPORT,
                details={"run_id": run_id},
            ),
        )
    return ApiResponse.success_response(BacktestMonthlyReturns.model_validate(payload))
