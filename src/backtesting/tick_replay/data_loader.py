from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from src.clients.mt5_market import Tick


class TickRepositoryPort(Protocol):
    def fetch_ticks(
        self,
        symbol: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int,
    ) -> list[tuple[Any, ...]]: ...


class TickReplayDataLoader:
    """Load persisted tick facts for tick-derived replay."""

    def __init__(self, repository: TickRepositoryPort) -> None:
        self._repository = repository

    def load_ticks(
        self,
        symbol: str,
        *,
        start: Optional[datetime],
        end: Optional[datetime],
        limit: int = 1_000_000,
    ) -> list[Tick]:
        rows = self._repository.fetch_ticks(symbol, start, end, limit)
        ticks = [self._row_to_tick(row) for row in rows]
        return sorted(ticks, key=lambda tick: (self._tick_order_msc(tick), tick.time))

    def _row_to_tick(self, row: tuple[Any, ...]) -> Tick:
        if len(row) < 9:
            raise ValueError("tick replay rows must use the full persisted tick contract")
        symbol, _price, bid, ask, last, volume, time_value, time_msc, flags = row[:9]
        parsed_msc = self._optional_int(time_msc)
        return Tick(
            symbol=str(symbol),
            bid=self._optional_float(bid),
            ask=self._optional_float(ask),
            last=self._optional_float(last),
            volume=float(volume or 0.0),
            time=self._as_utc_datetime(time_value, parsed_msc),
            time_msc=parsed_msc,
            flags=int(flags or 0),
        )

    @staticmethod
    def _optional_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        return float(value)

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        return int(value)

    @staticmethod
    def _as_utc_datetime(value: Any, time_msc: Optional[int]) -> datetime:
        if isinstance(value, datetime):
            dt = value
        elif value:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        elif time_msc is not None:
            dt = datetime.fromtimestamp(time_msc / 1000, tz=timezone.utc)
        else:
            raise ValueError("tick replay row missing time and time_msc")
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _tick_order_msc(tick: Tick) -> int:
        if tick.time_msc is not None:
            return int(tick.time_msc)
        return int(tick.time.timestamp() * 1000)
