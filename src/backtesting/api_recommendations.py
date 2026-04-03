from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import BaseModel, Field

from .api_execution import get_backtest_repo
from .runtime_store import backtest_runtime_store

logger = logging.getLogger(__name__)


class GenerateRecommendationRequest(BaseModel):
    walk_forward_run_id: str = Field(..., description="Walk-Forward 验证的 run_id")


class ApproveRejectRequest(BaseModel):
    reason: str = Field(default="", description="审核理由（可选）")


def get_recommendation(rec_id: str) -> Optional[Any]:
    cached = backtest_runtime_store.get_recommendation(rec_id)
    if cached is not None:
        return cached

    repo = get_backtest_repo()
    if repo is not None:
        recommendation = repo.fetch_recommendation(rec_id)
        if recommendation is not None:
            backtest_runtime_store.store_recommendation(rec_id, recommendation)
            return recommendation
    return None


def load_walk_forward_result(run_id: str) -> Optional[Any]:
    return backtest_runtime_store.get_walk_forward_result(run_id)


def get_signal_module() -> Optional[Any]:
    try:
        from src.api.deps import get_signal_service

        return get_signal_service()
    except Exception:
        logger.debug("SignalModule not available (standalone backtest mode)")
        return None
