"""Experiment API 组合根。"""

from fastapi import APIRouter

from .experiment_routes import router as experiment_router

router = APIRouter(prefix="/experiments", tags=["experiments"])
router.include_router(experiment_router)
