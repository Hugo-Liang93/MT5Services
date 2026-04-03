from .catalog import router as catalog_router
from .diagnostics import router as diagnostics_router
from .runtime import router as runtime_router

__all__ = [
    "catalog_router",
    "diagnostics_router",
    "runtime_router",
]
