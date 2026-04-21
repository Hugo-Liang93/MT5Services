"""GET /v1/backtest/history/{run_id}/metrics-summary — 运行级总指标（P11 Phase 1）。

前端 evaluation 页面顶部 summary 板的数据源。字段固定，无 alias，缺数据返 null。
"""

from __future__ import annotations

import logging
from typing import cast

from fastapi import APIRouter, Depends

from src.api import deps
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse
from src.readmodels.backtest_detail import BacktestDetailReadModel

from .schemas import BacktestMetricsSummary

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/history/{run_id}/metrics-summary",
    response_model=ApiResponse[BacktestMetricsSummary],
)
def get_metrics_summary(
    run_id: str,
    read_model: BacktestDetailReadModel = Depends(deps.get_backtest_detail_read_model),
) -> ApiResponse[BacktestMetricsSummary]:
    try:
        payload = read_model.build_metrics_summary(run_id)
    except Exception as exc:
        logger.exception("metrics-summary build failed for %s", run_id)
        return cast(
            ApiResponse[BacktestMetricsSummary],
            ApiResponse.error_response(
                error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
                error_message=str(exc),
                suggested_action=AIErrorAction.CONTACT_SUPPORT,
            ),
        )
    if payload is None:
        return cast(
            ApiResponse[BacktestMetricsSummary],
            ApiResponse.error_response(
                error_code=AIErrorCode.NOT_FOUND,
                error_message=f"Backtest run not found: {run_id}",
                suggested_action=AIErrorAction.CONTACT_SUPPORT,
                details={"run_id": run_id},
            ),
        )
    return ApiResponse.success_response(BacktestMetricsSummary.model_validate(payload))
