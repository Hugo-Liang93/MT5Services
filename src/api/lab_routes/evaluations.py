"""P11 Phase 5: GET /v1/lab/evaluations/{run_id} 单端点聚合。

替代前端对 detail 端点（metrics-summary / equity-curve / monthly-returns /
trade-structure / execution-realism / walk-forward / correlation-analysis）的多路
拼接，降低 BFF 复杂度 + 统一 freshness + 显式 capability。
"""

from __future__ import annotations

import logging
from typing import Optional, cast

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from src.api import deps
from src.api.backtest_routes.detail.schemas import (
    BacktestEquityCurve,
    BacktestMetricsSummary,
    BacktestMonthlyReturns,
    CorrelationAnalysisDetail,
    ExecutionRealism,
    FreshnessBlock,
    TradeStructure,
    WalkForwardDetail,
)
from src.api.error_codes import AIErrorAction, AIErrorCode
from src.api.schemas import ApiResponse
from src.readmodels.lab_evaluation import LabEvaluationReadModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["lab"])


class LabEvaluationCapability(BaseModel):
    """每子块可用性状态：'ready' / 'partial' / 'missing'。

    前端按 state 决定 UI：
    - ready → 正常渲染
    - partial → 渲染但显示"部分数据"提示
    - missing → 渲染"暂无"占位
    """

    metrics_summary: str
    equity_curve: str
    monthly_returns: str
    trade_structure: str
    execution_realism: str
    walk_forward: str
    correlation_analysis: str


class LabEvaluationEnvelope(BaseModel):
    """Lab 评估页单端点聚合响应。"""

    run_id: str
    observed_at: str
    freshness: FreshnessBlock = Field(
        ..., description="envelope 级 freshness（取所有子块最老的 data_updated_at）"
    )
    capability: LabEvaluationCapability
    metrics_summary: BacktestMetricsSummary
    equity_curve: Optional[BacktestEquityCurve] = None
    monthly_returns: Optional[BacktestMonthlyReturns] = None
    trade_structure: Optional[TradeStructure] = None
    execution_realism: Optional[ExecutionRealism] = None
    walk_forward: Optional[WalkForwardDetail] = None
    correlation_analysis: Optional[CorrelationAnalysisDetail] = None


@router.get(
    "/lab/evaluations/{run_id}",
    response_model=ApiResponse[LabEvaluationEnvelope],
    summary="Lab 评估页聚合读端点（P11 Phase 5）",
    description=(
        "单端点返回策略评估页所需全部字段 + capability 状态：metrics / "
        "equity_curve / monthly_returns / trade_structure / execution_realism / "
        "walk_forward / correlation_analysis。前端不再需要多路拼接。"
    ),
)
def lab_evaluation(
    run_id: str,
    read_model: LabEvaluationReadModel = Depends(deps.get_lab_evaluation_read_model),
) -> ApiResponse[LabEvaluationEnvelope]:
    try:
        payload = read_model.build(run_id)
    except Exception as exc:
        logger.exception("lab/evaluations build failed for %s", run_id)
        return cast(
            ApiResponse[LabEvaluationEnvelope],
            ApiResponse.error_response(
                error_code=AIErrorCode.INTERNAL_SERVER_ERROR,
                error_message=str(exc),
                suggested_action=AIErrorAction.CONTACT_SUPPORT,
            ),
        )
    if payload is None:
        return cast(
            ApiResponse[LabEvaluationEnvelope],
            ApiResponse.error_response(
                error_code=AIErrorCode.NOT_FOUND,
                error_message=f"Backtest run not found: {run_id}",
                suggested_action=AIErrorAction.CONTACT_SUPPORT,
                details={"run_id": run_id},
            ),
        )
    return ApiResponse.success_response(LabEvaluationEnvelope.model_validate(payload))
