"""Shared factory: (symbol, field) → callable(date) → Optional[float] from
daily_external_ohlc, with per-instance date cache.

All FeatureProviders that need daily external OHLCV use this — no provider
should re-implement DB query + caching.

Future intraday extension: add `make_intraday_field_lookup(symbol, field)`
returning callable(datetime) → Optional[float] from a future
intraday_external_ohlc table. Same caching pattern.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable, Dict, Optional

SUPPORTED_FIELDS: frozenset[str] = frozenset(
    {"open", "high", "low", "close", "volume"}
)


def _default_writer_factory() -> Any:
    """Lazy-construct TimescaleWriter for the live environment.

    Isolated so tests can inject a mock writer without importing src.config.database.
    """
    from src.config.database import load_db_settings
    from src.persistence.db import TimescaleWriter

    return TimescaleWriter(load_db_settings("live"))


def make_daily_field_lookup(
    symbol: str,
    *,
    field: str = "close",
    _writer_factory: Callable[[], Any] = _default_writer_factory,
) -> Callable[[date], Optional[float]]:
    """Return a callable that fetches `field` for `symbol` on a given date.

    The returned callable caches per (symbol, field, date) so repeated lookups
    in the same FeatureProvider compute() pass hit DB only once per date.

    Raises ValueError immediately if `field` is not a daily_external_ohlc column.

    `_writer_factory` is for test injection; production callers omit it.
    """
    if field not in SUPPORTED_FIELDS:
        raise ValueError(
            f"unknown field '{field}'; supported: {sorted(SUPPORTED_FIELDS)}"
        )

    writer = _writer_factory()
    cache: Dict[date, Optional[float]] = {}

    sql = (
        f"SELECT {field} FROM daily_external_ohlc "  # nosec B608: field whitelisted above
        "WHERE symbol=%s AND date=%s"
    )

    def lookup(d: date) -> Optional[float]:
        if d in cache:
            return cache[d]
        with writer.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (symbol, d))
            row = cur.fetchone()
            cache[d] = float(row[0]) if row and row[0] is not None else None
        return cache[d]

    return lookup
