"""Experiment Registry REST API 端点。

被动追踪实验从 Research → Backtest → Demo Validation → Live 的生命周期（参 ADR-010）。
各模块通过 experiment_id 关联，此处只提供查询和手动管理接口。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from src.api import deps
from src.api.schemas import ApiResponse

logger = logging.getLogger(__name__)
router = APIRouter()


class CreateExperimentRequest(BaseModel):
    experiment_id: Optional[str] = Field(None, description="自定义 ID（留空自动生成）")
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    notes: Optional[str] = None


def _get_experiment_repo():  # type: ignore[no-untyped-def]
    """获取共享的 ExperimentRepository（来自 container.storage_writer）。

    禁止在此构造独立 TimescaleWriter——历史教训见 `deps.get_experiment_repo` 注释。
    """
    try:
        return deps.get_experiment_repo()
    except Exception:
        logger.debug("ExperimentRepository not available", exc_info=True)
        return None


@router.post("/", response_model=ApiResponse)
def create_experiment(req: CreateExperimentRequest) -> ApiResponse:
    """创建新实验。"""
    from src.utils.experiment import generate_experiment_id

    repo = _get_experiment_repo()
    if repo is None:
        return ApiResponse(success=False, error="Database not available")

    exp_id = req.experiment_id or generate_experiment_id()
    repo.create(
        exp_id,
        symbol=req.symbol,
        timeframe=req.timeframe,
    )
    return ApiResponse(success=True, data={"experiment_id": exp_id})


@router.get("/", response_model=ApiResponse)
def list_experiments(
    status: Optional[str] = Query(None, description="按状态过滤"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ApiResponse:
    """查询实验列表。"""
    repo = _get_experiment_repo()
    if repo is None:
        return ApiResponse(success=False, error="Database not available")

    experiments = repo.list_experiments(status=status, limit=limit, offset=offset)
    return ApiResponse(success=True, data=experiments)


@router.get("/{experiment_id}", response_model=ApiResponse)
def get_experiment(experiment_id: str) -> ApiResponse:
    """查询单个实验详情。"""
    repo = _get_experiment_repo()
    if repo is None:
        return ApiResponse(success=False, error="Database not available")

    exp = repo.fetch(experiment_id)
    if exp is None:
        return ApiResponse(success=False, error=f"Experiment {experiment_id} not found")
    return ApiResponse(success=True, data=exp)


@router.get("/{experiment_id}/timeline", response_model=ApiResponse)
def get_experiment_timeline(experiment_id: str) -> ApiResponse:
    """查询实验阶段时间线（含各阶段指标快照）。"""
    repo = _get_experiment_repo()
    if repo is None:
        return ApiResponse(success=False, error="Database not available")

    exp = repo.fetch(experiment_id)
    if exp is None:
        return ApiResponse(success=False, error=f"Experiment {experiment_id} not found")

    # 构建时间线
    phases = []
    if exp.get("mining_run_id"):
        phases.append(
            {
                "phase": "research",
                "mining_run_id": exp["mining_run_id"],
            }
        )
    if exp.get("backtest_run_ids"):
        phases.append(
            {
                "phase": "backtest",
                "run_ids": exp["backtest_run_ids"],
                "sharpe": exp.get("backtest_sharpe"),
                "win_rate": exp.get("backtest_win_rate"),
            }
        )
    if exp.get("recommendation_id"):
        phases.append(
            {
                "phase": "recommendation",
                "rec_id": exp["recommendation_id"],
            }
        )
    if (
        exp.get("status") == "demo_validation"
        or exp.get("demo_validation_sharpe") is not None
    ):
        phases.append(
            {
                "phase": "demo_validation",
                "sharpe": exp.get("demo_validation_sharpe"),
                "win_rate": exp.get("demo_validation_win_rate"),
                "validation_passed": exp.get("validation_passed"),
            }
        )
    if exp.get("status") == "live":
        phases.append({"phase": "live"})

    return ApiResponse(
        success=True,
        data={
            "experiment_id": experiment_id,
            "status": exp["status"],
            "created_at": exp["created_at"],
            "updated_at": exp["updated_at"],
            "phases": phases,
        },
    )


@router.post("/{experiment_id}/abandon", response_model=ApiResponse)
def abandon_experiment(experiment_id: str) -> ApiResponse:
    """标记实验为废弃。"""
    repo = _get_experiment_repo()
    if repo is None:
        return ApiResponse(success=False, error="Database not available")

    exp = repo.fetch(experiment_id)
    if exp is None:
        return ApiResponse(success=False, error=f"Experiment {experiment_id} not found")

    repo.mark_abandoned(experiment_id)
    return ApiResponse(
        success=True, data={"experiment_id": experiment_id, "status": "abandoned"}
    )
