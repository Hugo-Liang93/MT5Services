"""回测 API 组合根。"""

from __future__ import annotations

from fastapi import APIRouter

from .backtest_routes import (
    config_router,
    detail_router,
    jobs_router,
    recommendations_router,
)

router = APIRouter(prefix="/backtest", tags=["backtest"])

router.include_router(config_router)
router.include_router(jobs_router)
router.include_router(detail_router)
router.include_router(recommendations_router)

__all__ = ["router"]
