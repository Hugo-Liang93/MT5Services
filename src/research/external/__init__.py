"""External daily-resolution data sources for research feature providers.

Public API: ExternalDataSource protocol + registry. Concrete sources
(YFinanceClient, future FredClient/PolygonClient) live as siblings.
"""

from src.research.external.protocol import (
    DailyBar,
    ExternalDataSource,
    UnknownSourceError,
    get_source,
    list_registered_sources,
    register_source,
)

__all__ = [
    "DailyBar",
    "ExternalDataSource",
    "UnknownSourceError",
    "get_source",
    "list_registered_sources",
    "register_source",
]

# Import side-effect: registers yfinance under the source registry.
from src.research.external import yfinance_client as _yfinance_client  # noqa: F401
