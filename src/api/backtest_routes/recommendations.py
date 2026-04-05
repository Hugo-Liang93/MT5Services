from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter

from src.api.schemas import ApiResponse
from src.api.backtest_routes import helpers as api_recommendations

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/recommendations/generate", response_model=ApiResponse)
async def generate_recommendation(
    request: api_recommendations.GenerateRecommendationRequest,
) -> ApiResponse:
    try:
        wf_result = api_recommendations.load_walk_forward_result(
            request.walk_forward_run_id
        )
        if wf_result is None:
            return ApiResponse(
                success=False,
                error=f"Walk-Forward 结果 {request.walk_forward_run_id} 未找到",
            )

        from src.backtesting.optimization.recommendation import (
            RecommendationEngine,
            load_current_signal_config,
        )

        wf_timeframe = getattr(
            getattr(getattr(wf_result, "config", None), "base_config", None),
            "timeframe",
            None,
        )
        current_params, _, current_affinities = load_current_signal_config(wf_timeframe)
        engine = RecommendationEngine()
        recommendation = engine.generate(
            wf_result=wf_result,
            source_run_id=request.walk_forward_run_id,
            current_strategy_params=current_params,
            current_regime_affinities=current_affinities,
            timeframe=wf_timeframe,
        )

        repo = api_recommendations.get_backtest_repo()
        if repo is not None:
            repo.save_recommendation(recommendation)
        api_recommendations.backtest_runtime_store.store_recommendation(
            recommendation.rec_id,
            recommendation,
        )
        return ApiResponse(success=True, data=recommendation.to_dict())
    except ValueError as exc:
        return ApiResponse(success=False, error=str(exc))
    except Exception as exc:
        logger.exception("Failed to generate recommendation")
        return ApiResponse(success=False, error=str(exc))


@router.get("/recommendations", response_model=ApiResponse)
async def list_recommendations(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> ApiResponse:
    try:
        repo = api_recommendations.get_backtest_repo()
        if repo is None:
            return ApiResponse(success=False, error="Database not available")
        recommendations = repo.fetch_recommendations(
            status=status,
            limit=limit,
            offset=offset,
        )
        return ApiResponse(
            success=True,
            data=[recommendation.to_dict() for recommendation in recommendations],
        )
    except Exception as exc:
        return ApiResponse(success=False, error=str(exc))


@router.get("/recommendations/{rec_id}", response_model=ApiResponse)
async def get_recommendation(rec_id: str) -> ApiResponse:
    recommendation = api_recommendations.get_recommendation(rec_id)
    if recommendation is None:
        return ApiResponse(success=False, error=f"推荐 {rec_id} 未找到")
    return ApiResponse(success=True, data=recommendation.to_dict())


@router.post("/recommendations/{rec_id}/approve", response_model=ApiResponse)
async def approve_recommendation(
    rec_id: str,
    request: api_recommendations.ApproveRejectRequest,
) -> ApiResponse:
    del request
    from src.backtesting.models import RecommendationStatus

    recommendation = api_recommendations.get_recommendation(rec_id)
    if recommendation is None:
        return ApiResponse(success=False, error=f"推荐 {rec_id} 未找到")
    if recommendation.status != RecommendationStatus.PENDING:
        return ApiResponse(
            success=False,
            error=f"推荐状态为 {recommendation.status.value}，仅 pending 可审核",
        )

    recommendation.status = RecommendationStatus.APPROVED
    recommendation.approved_at = datetime.now(timezone.utc)

    repo = api_recommendations.get_backtest_repo()
    if repo is not None:
        repo.update_recommendation(recommendation)
    api_recommendations.backtest_runtime_store.store_recommendation(
        rec_id,
        recommendation,
    )
    return ApiResponse(success=True, data=recommendation.to_dict())


@router.post("/recommendations/{rec_id}/reject", response_model=ApiResponse)
async def reject_recommendation(
    rec_id: str,
    request: api_recommendations.ApproveRejectRequest,
) -> ApiResponse:
    del request
    from src.backtesting.models import RecommendationStatus

    recommendation = api_recommendations.get_recommendation(rec_id)
    if recommendation is None:
        return ApiResponse(success=False, error=f"推荐 {rec_id} 未找到")
    if recommendation.status != RecommendationStatus.PENDING:
        return ApiResponse(
            success=False,
            error=f"推荐状态为 {recommendation.status.value}，仅 pending 可拒绝",
        )

    recommendation.status = RecommendationStatus.REJECTED
    repo = api_recommendations.get_backtest_repo()
    if repo is not None:
        repo.update_recommendation(recommendation)
    api_recommendations.backtest_runtime_store.store_recommendation(
        rec_id,
        recommendation,
    )
    return ApiResponse(success=True, data=recommendation.to_dict())


@router.post("/recommendations/{rec_id}/apply", response_model=ApiResponse)
async def apply_recommendation(rec_id: str) -> ApiResponse:
    from src.backtesting.optimization.recommendation import ConfigApplicator

    recommendation = api_recommendations.get_recommendation(rec_id)
    if recommendation is None:
        return ApiResponse(success=False, error=f"推荐 {rec_id} 未找到")

    try:
        signal_module = api_recommendations.get_signal_module()
        applicator = ConfigApplicator(signal_module=signal_module)
        backup_path = applicator.apply(recommendation)

        repo = api_recommendations.get_backtest_repo()
        if repo is not None:
            try:
                repo.update_recommendation(recommendation)
            except Exception:
                logger.warning(
                    "推荐 %s 已应用但 DB 更新失败，状态可能不一致",
                    rec_id,
                    exc_info=True,
                )

        api_recommendations.backtest_runtime_store.store_recommendation(
            rec_id,
            recommendation,
        )
        return ApiResponse(
            success=True,
            data={**recommendation.to_dict(), "backup_path": backup_path},
        )
    except ValueError as exc:
        return ApiResponse(success=False, error=str(exc))
    except Exception as exc:
        logger.exception("Failed to apply recommendation %s", rec_id)
        return ApiResponse(success=False, error=str(exc))


@router.post("/recommendations/{rec_id}/rollback", response_model=ApiResponse)
async def rollback_recommendation(rec_id: str) -> ApiResponse:
    from src.backtesting.optimization.recommendation import ConfigApplicator

    recommendation = api_recommendations.get_recommendation(rec_id)
    if recommendation is None:
        return ApiResponse(success=False, error=f"推荐 {rec_id} 未找到")

    try:
        signal_module = api_recommendations.get_signal_module()
        applicator = ConfigApplicator(signal_module=signal_module)
        applicator.rollback(recommendation)

        repo = api_recommendations.get_backtest_repo()
        if repo is not None:
            try:
                repo.update_recommendation(recommendation)
            except Exception:
                logger.warning("推荐 %s 已回滚但 DB 更新失败", rec_id, exc_info=True)

        api_recommendations.backtest_runtime_store.store_recommendation(
            rec_id,
            recommendation,
        )
        return ApiResponse(success=True, data=recommendation.to_dict())
    except ValueError as exc:
        return ApiResponse(success=False, error=str(exc))
    except Exception as exc:
        logger.exception("Failed to rollback recommendation %s", rec_id)
        return ApiResponse(success=False, error=str(exc))
