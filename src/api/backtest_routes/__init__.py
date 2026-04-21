from .config import router as config_router
from .detail import router as detail_router
from .jobs import router as jobs_router
from .recommendations import router as recommendations_router

__all__ = [
    "config_router",
    "detail_router",
    "jobs_router",
    "recommendations_router",
]
