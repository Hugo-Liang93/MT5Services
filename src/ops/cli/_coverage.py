from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


@dataclass(frozen=True)
class TimeframeCoverage:
    timeframe: str
    count: int
    first: datetime | None
    last: datetime | None

    @property
    def covers_requested_window(self) -> bool:
        return self.count > 0 and self.first is not None and self.last is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "timeframe": self.timeframe,
            "count": self.count,
            "first": self.first.isoformat() if self.first else None,
            "last": self.last.isoformat() if self.last else None,
        }


def ensure_ohlc_data_coverage(
    *,
    symbol: str,
    timeframes: Iterable[str],
    start: datetime,
    end: datetime,
    auto_backfill: bool,
) -> dict[str, TimeframeCoverage]:
    from src.config.database import load_db_settings
    from src.ops.cli.backfill_ohlc import backfill
    from src.persistence.db import TimescaleWriter

    tf_list = [str(tf).strip().upper() for tf in timeframes if str(tf).strip()]
    writer = TimescaleWriter(load_db_settings(), min_conn=1, max_conn=2)
    try:
        coverage_before = _load_coverage(writer, symbol=symbol, timeframes=tf_list)
    finally:
        writer.close()

    missing = [
        tf
        for tf, info in coverage_before.items()
        if info.first is None or info.last is None or info.first > start or info.last < end
    ]
    if missing and auto_backfill:
        backfill(symbol=symbol, timeframes=missing, start=start, end=end)
        writer = TimescaleWriter(load_db_settings(), min_conn=1, max_conn=2)
        try:
            return _load_coverage(writer, symbol=symbol, timeframes=tf_list)
        finally:
            writer.close()
    return coverage_before


def _load_coverage(
    writer: "TimescaleWriter",
    *,
    symbol: str,
    timeframes: Iterable[str],
) -> dict[str, TimeframeCoverage]:
    coverage: dict[str, TimeframeCoverage] = {}
    with writer.connection() as conn, conn.cursor() as cur:
        for tf in timeframes:
            cur.execute(
                "SELECT COUNT(*), MIN(time), MAX(time) FROM ohlc "
                "WHERE symbol = %s AND timeframe = %s",
                (symbol, tf),
            )
            row = cur.fetchone()
            coverage[tf] = TimeframeCoverage(
                timeframe=tf,
                count=int(row[0] or 0),
                first=row[1],
                last=row[2],
            )
    return coverage
