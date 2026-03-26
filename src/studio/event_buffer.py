"""Thread-safe ring buffer for Studio events.

Stores the most recent N events in memory.  No persistence — Studio events
are a presentation-layer concept; the underlying business events (signals,
trades, alerts) are already persisted in their respective DB tables.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any


class EventBuffer:
    """Fixed-size, thread-safe, append-only ring buffer."""

    __slots__ = ("_buffer", "_lock")

    def __init__(self, max_size: int = 200) -> None:
        self._buffer: deque[dict[str, Any]] = deque(maxlen=max_size)
        self._lock = threading.Lock()

    def append(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._buffer.append(event)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the *limit* most recent events, oldest first."""
        with self._lock:
            items = list(self._buffer)
        return items[-limit:] if limit < len(items) else items

    def __len__(self) -> int:
        with self._lock:
            return len(self._buffer)
