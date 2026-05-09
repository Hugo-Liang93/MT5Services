from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Callable, Optional

from src.market.tick_features.models import TickFeatureSnapshot
from src.utils.common import same_listener_reference


class TickFeatureBus:
    """Bounded FIFO bus for tick-derived feature snapshots."""

    def __init__(self, maxlen: int = 4096) -> None:
        self._maxlen = max(1, int(maxlen))
        self._queue: deque[TickFeatureSnapshot] = deque()
        self._lock = Lock()
        self._dropped_snapshots = 0
        self._last_publish_at: Optional[datetime] = None
        self._listeners: list[Callable[[TickFeatureSnapshot], None]] = []
        self._failed_dispatches = 0
        self._last_error: Optional[str] = None

    def add_listener(self, listener: Callable[[TickFeatureSnapshot], None]) -> None:
        with self._lock:
            if any(same_listener_reference(item, listener) for item in self._listeners):
                return
            self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[TickFeatureSnapshot], None]) -> None:
        with self._lock:
            self._listeners = [
                item
                for item in self._listeners
                if not same_listener_reference(item, listener)
            ]

    def publish(self, snapshot: TickFeatureSnapshot) -> None:
        with self._lock:
            listeners = list(self._listeners)
            if not listeners:
                if len(self._queue) >= self._maxlen:
                    self._queue.popleft()
                    self._dropped_snapshots += 1
                self._queue.append(snapshot)
            self._last_publish_at = datetime.now(timezone.utc)
        for listener in listeners:
            try:
                listener(snapshot)
            except Exception as exc:
                with self._lock:
                    self._failed_dispatches += 1
                    self._last_error = str(exc)

    def drain(self, max_items: int) -> list[TickFeatureSnapshot]:
        limit = max(0, int(max_items))
        drained: list[TickFeatureSnapshot] = []
        with self._lock:
            while self._queue and len(drained) < limit:
                drained.append(self._queue.popleft())
        return drained

    def stats(self) -> dict[str, object]:
        with self._lock:
            return {
                "queue_depth": len(self._queue),
                "dropped_snapshots": self._dropped_snapshots,
                "last_publish_at": (
                    self._last_publish_at.isoformat()
                    if self._last_publish_at is not None
                    else None
                ),
                "listeners": len(self._listeners),
                "failed_dispatches": self._failed_dispatches,
                "last_error": self._last_error,
            }
