from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

from src.market.tick_features.models import TickFeatureSnapshot


@dataclass(frozen=True)
class TickFeatureHealth:
    symbol: str
    status: str
    last_snapshot_at: Optional[datetime]
    last_window_end_msc: Optional[int]
    snapshot_age_seconds: Optional[float]
    queue_depth: int
    dropped_snapshots: int
    last_reasons: tuple[str, ...]

    @property
    def blocking(self) -> bool:
        return self.status in {"stale", "blocked", "missing"}

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "blocking": self.blocking,
            "last_snapshot_at": (
                self.last_snapshot_at.isoformat()
                if self.last_snapshot_at is not None
                else None
            ),
            "last_window_end_msc": self.last_window_end_msc,
            "snapshot_age_seconds": self.snapshot_age_seconds,
            "queue_depth": self.queue_depth,
            "dropped_snapshots": self.dropped_snapshots,
            "last_reasons": list(self.last_reasons),
        }


class TickFeatureHealthStore:
    """Owns tick feature health facts per symbol."""

    def __init__(self, max_snapshot_age_seconds: float) -> None:
        self._max_snapshot_age_seconds = float(max_snapshot_age_seconds)
        self._lock = Lock()
        self._latest_by_symbol: dict[str, TickFeatureSnapshot] = {}
        self._last_bus_stats: dict[str, object] = {
            "queue_depth": 0,
            "dropped_snapshots": 0,
        }

    def update_from_snapshot(
        self,
        snapshot: TickFeatureSnapshot,
        bus_stats: dict[str, object],
    ) -> None:
        with self._lock:
            self._latest_by_symbol[snapshot.symbol] = snapshot
            self._last_bus_stats = dict(bus_stats)

    def health_for(
        self,
        symbol: str,
        *,
        now: Optional[datetime] = None,
        bus_stats: Optional[dict[str, object]] = None,
    ) -> TickFeatureHealth:
        now = self._as_utc(now or datetime.now(timezone.utc))
        with self._lock:
            snapshot = self._latest_by_symbol.get(symbol)
            stats = dict(bus_stats or self._last_bus_stats)
        if snapshot is None:
            return TickFeatureHealth(
                symbol=symbol,
                status="missing",
                last_snapshot_at=None,
                last_window_end_msc=None,
                snapshot_age_seconds=None,
                queue_depth=int(stats.get("queue_depth") or 0),
                dropped_snapshots=int(stats.get("dropped_snapshots") or 0),
                last_reasons=("no_snapshot",),
            )

        age = max(0.0, (now - self._as_utc(snapshot.generated_at)).total_seconds())
        if age > self._max_snapshot_age_seconds:
            status = "stale"
            reasons = ("snapshot_stale",)
        else:
            status = snapshot.status
            reasons = snapshot.reasons
        return TickFeatureHealth(
            symbol=symbol,
            status=status,
            last_snapshot_at=snapshot.generated_at,
            last_window_end_msc=snapshot.window_end_msc,
            snapshot_age_seconds=age,
            queue_depth=int(stats.get("queue_depth") or 0),
            dropped_snapshots=int(stats.get("dropped_snapshots") or 0),
            last_reasons=reasons,
        )

    def snapshot(
        self,
        *,
        now: Optional[datetime] = None,
        bus_stats: Optional[dict[str, object]] = None,
    ) -> dict[str, dict[str, object]]:
        with self._lock:
            symbols = list(self._latest_by_symbol)
        return {
            symbol: self.health_payload(symbol, now=now, bus_stats=bus_stats)
            for symbol in symbols
        }

    def health_payload(
        self,
        symbol: str,
        *,
        now: Optional[datetime] = None,
        bus_stats: Optional[dict[str, object]] = None,
    ) -> dict[str, object]:
        return self.health_for(symbol, now=now, bus_stats=bus_stats).to_dict()

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
