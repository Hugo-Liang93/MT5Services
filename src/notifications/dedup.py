"""In-memory TTL-based deduplication for notification events.

Prevents alert storms: the same ``dedup_key`` observed within its severity's
TTL window is suppressed until the window elapses. Suppression counts are
tracked per key so downstream can emit a periodic digest if desired.

Thread-safe for concurrent submitters (dispatcher) + periodic cleaner.
Sized with a hard cap to guarantee O(1) memory even if classifiers churn
keys faster than TTLs evict.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True)
class DedupVerdict:
    allowed: bool
    suppressed_count: int  # Cumulative suppressions for this key since last allow.


class Deduper:
    def __init__(self, *, max_entries: int = 4096) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be > 0")
        self._lock = threading.Lock()
        # OrderedDict by insertion gives O(1) LRU eviction.
        self._last_sent_at: "OrderedDict[str, float]" = OrderedDict()
        self._suppressed_count: dict[str, int] = {}
        self._max_entries = max_entries

    def check(
        self, dedup_key: str, ttl_seconds: int, *, now: float | None = None
    ) -> DedupVerdict:
        """Decide whether ``dedup_key`` should be sent right now.

        - ``ttl_seconds <= 0``: dedup disabled, always allowed (fresh send
          still resets the suppression counter).
        - First observation: allowed, recorded.
        - Within TTL: suppressed, counter incremented.
        - After TTL: allowed, counter reset.
        """
        if not dedup_key:
            raise ValueError("dedup_key cannot be empty")
        current = now if now is not None else monotonic()
        with self._lock:
            if ttl_seconds <= 0:
                self._record_allowed(dedup_key, current)
                return DedupVerdict(allowed=True, suppressed_count=0)
            last = self._last_sent_at.get(dedup_key)
            if last is None or (current - last) >= ttl_seconds:
                self._record_allowed(dedup_key, current)
                return DedupVerdict(allowed=True, suppressed_count=0)
            self._suppressed_count[dedup_key] = (
                self._suppressed_count.get(dedup_key, 0) + 1
            )
            return DedupVerdict(
                allowed=False, suppressed_count=self._suppressed_count[dedup_key]
            )

    def _record_allowed(self, dedup_key: str, current: float) -> None:
        self._last_sent_at[dedup_key] = current
        self._last_sent_at.move_to_end(dedup_key)
        self._suppressed_count.pop(dedup_key, None)
        while len(self._last_sent_at) > self._max_entries:
            evicted_key, _ = self._last_sent_at.popitem(last=False)
            self._suppressed_count.pop(evicted_key, None)

    def clear_expired(self, *, now: float | None = None, ttl_seconds: int) -> int:
        """Drop entries older than ``ttl_seconds`` to keep memory bounded.

        Returns the number of entries evicted. The dispatcher periodically
        calls this with the largest configured TTL (typically info_ttl),
        since any entry older than that is guaranteed to never suppress again.
        """
        if ttl_seconds <= 0:
            return 0
        current = now if now is not None else monotonic()
        evicted = 0
        with self._lock:
            # Sorted insertion order → oldest are at the front.
            stale_keys: list[str] = []
            for key, sent_at in self._last_sent_at.items():
                if (current - sent_at) >= ttl_seconds:
                    stale_keys.append(key)
                else:
                    break
            for key in stale_keys:
                self._last_sent_at.pop(key, None)
                self._suppressed_count.pop(key, None)
                evicted += 1
        return evicted

    def pending_suppressions(self) -> dict[str, int]:
        """Snapshot of currently-suppressed keys (for /status reporting)."""
        with self._lock:
            return dict(self._suppressed_count)

    def size(self) -> int:
        with self._lock:
            return len(self._last_sent_at)
