"""Paper Trading API 组合根。"""

from fastapi import APIRouter

from .paper_trading_routes import router as paper_trading_router

router = APIRouter(prefix="/paper-trading", tags=["paper-trading"])
router.include_router(paper_trading_router)
