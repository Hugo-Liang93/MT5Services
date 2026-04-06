"""Research API 组合根。"""

from fastapi import APIRouter

from .research_routes import router as research_router

router = APIRouter(prefix="/research", tags=["research"])
router.include_router(research_router)
