"""
Timescale schema and SQL statements grouped by module.
"""

from .ticks import DDL as TICKS_DDL, INSERT_SQL as INSERT_TICKS_SQL
from .quotes import DDL as QUOTES_DDL, INSERT_SQL as INSERT_QUOTES_SQL
from .ohlc import (
    DDL as OHLC_DDL,
    INSERT_SQL as INSERT_OHLC_SQL,
    UPSERT_SQL as UPSERT_OHLC_SQL,
)
from .intrabar import DDL as INTRABAR_DDL, INSERT_SQL as INSERT_INTRABAR_SQL

DDL_STATEMENTS = [
    TICKS_DDL,
    QUOTES_DDL,
    OHLC_DDL,
    INTRABAR_DDL,
]

__all__ = [
    "DDL_STATEMENTS",
    "INSERT_TICKS_SQL",
    "INSERT_QUOTES_SQL",
    "INSERT_OHLC_SQL",
    "UPSERT_OHLC_SQL",
    "INSERT_INTRABAR_SQL",
]
