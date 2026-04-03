from .config import router as config_router
from .dashboard import router as dashboard_router
from .strategies import router as strategies_router
from .streams import router as streams_router

__all__ = [
    "config_router",
    "dashboard_router",
    "strategies_router",
    "streams_router",
]
