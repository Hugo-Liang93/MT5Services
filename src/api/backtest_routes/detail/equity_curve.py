"""GET /v1/backtest/history/{run_id}/equity-curve — 净值 + 回撤序列（P11 Phase 1）。"""

from __future__ import annotations

import logging
from typing import cast

from fastapi import APIRouter, Depends

from src.api import deps
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse
from src.readmodels.backtest_detail import BacktestDetailReadModel

from .schemas import BacktestEquityCurve

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/history/{run_id}/equity-curve",
    response_model=ApiResponse[BacktestEquityCurve],
)
def get_equity_curve(
    run_id: str,
    read_model: BacktestDetailReadModel = Depends(deps.get_backtest_detail_read_model),
) -> ApiResponse[BacktestEquityCurve]:
    try:
        payload = read_model.build_equity_curve(run_id)
    except Exception as exc:
        logger.exception("equity-curve build failed for %s", run_id)
        return cast(
            ApiResponse[BacktestEquityCurve],
            ApiResponse.error_response(
                error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
                error_message=str(exc),
                suggested_action=AIErrorAction.CONTACT_SUPPORT,
            ),
        )
    if payload is None:
        return cast(
            ApiResponse[BacktestEquityCurve],
            ApiResponse.error_response(
                error_code=AIErrorCode.NOT_FOUND,
                error_message=f"Backtest run not found: {run_id}",
                suggested_action=AIErrorAction.CONTACT_SUPPORT,
                details={"run_id": run_id},
            ),
        )
    return ApiResponse.success_response(BacktestEquityCurve.model_validate(payload))
