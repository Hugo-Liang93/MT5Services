from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Protocol

from .models import SignalRecord

if TYPE_CHECKING:
    from src.persistence.db import TimescaleWriter


class SignalRepository(Protocol):
    def append(self, record: SignalRecord) -> None:
        ...

    def recent(
        self,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy: Optional[str] = None,
        action: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict]:
        ...

    def summary(self, *, hours: int = 24) -> list[dict]:
        ...


class TimescaleSignalRepository:
    def __init__(self, db_writer: "TimescaleWriter"):
        self.db_writer = db_writer

    def append(self, record: SignalRecord) -> None:
        self.db_writer.write_signal_events([record.to_row()])

    def recent(
        self,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy: Optional[str] = None,
        action: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict]:
        rows = self.db_writer.fetch_signal_events(
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            action=action,
            limit=limit,
        )
        return [
            {
                "generated_at": row[0].isoformat() if row[0] else None,
                "signal_id": row[1],
                "symbol": row[2],
                "timeframe": row[3],
                "strategy": row[4],
                "action": row[5],
                "confidence": row[6],
                "reason": row[7],
                "used_indicators": row[8] or [],
                "indicators_snapshot": row[9] or {},
                "metadata": row[10] or {},
            }
            for row in rows
        ]

    def summary(self, *, hours: int = 24) -> list[dict]:
        rows = self.db_writer.summarize_signal_events(hours=hours)
        return [
            {
                "symbol": row[0],
                "timeframe": row[1],
                "strategy": row[2],
                "action": row[3],
                "count": int(row[4] or 0),
                "avg_confidence": row[5],
                "last_seen_at": row[6].isoformat() if row[6] else None,
            }
            for row in rows
        ]
