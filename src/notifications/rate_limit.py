"""Token bucket rate limiter for outbound notifications.

Telegram Bot API caps at ~30 msg/sec globally and ~1 msg/sec per chat.
This limiter models both levels as fractional-refill token buckets so bursts
during startup or batch alerts don't trigger 429 responses.

Decision semantics:
- ``acquire(chat_id)`` returns an ``Verdict`` with:
    - ``allowed=True``  — one global token + one per-chat token consumed.
    - ``allowed=False`` — either bucket drained, with ``blocked_by`` indicating
      which one. Callers decide whether to downgrade (INFO/WARN drop, CRITICAL
      persists in outbox and retries later).
- Refill is lazy and happens on every ``acquire`` call — no background timer.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True)
class RateLimitVerdict:
    allowed: bool
    blocked_by: str | None  # None | "global" | "per_chat"


class _TokenBucket:
    __slots__ = ("_capacity", "_refill_per_second", "_tokens", "_last_refill")

    def __init__(self, capacity: int, refill_per_second: float) -> None:
        self._capacity = float(capacity)
        self._refill_per_second = float(refill_per_second)
        self._tokens = float(capacity)
        # None until first call. Using a sentinel rather than monotonic() at
        # construction keeps the bucket ``now``-agnostic — callers drive the
        # clock, which is essential for deterministic tests and for acquire()
        # usage where ``now`` may be monotonic() at a moment different from
        # when the bucket was instantiated (e.g. lazily created per chat_id).
        self._last_refill: float | None = None

    def _refill(self, now: float) -> None:
        if self._last_refill is None:
            self._last_refill = now
            return
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._tokens = min(
            self._capacity, self._tokens + elapsed * self._refill_per_second
        )
        self._last_refill = now

    def try_acquire(self, *, now: float) -> bool:
        self._refill(now)
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def available(self, *, now: float) -> float:
        self._refill(now)
        return self._tokens


class RateLimiter:
    """Two-level limiter: one global bucket + one bucket per chat_id."""

    def __init__(
        self,
        *,
        global_per_minute: int,
        per_chat_per_minute: int,
    ) -> None:
        if global_per_minute <= 0 or per_chat_per_minute <= 0:
            raise ValueError("rate limits must be > 0")
        self._lock = threading.Lock()
        self._global = _TokenBucket(
            capacity=global_per_minute,
            refill_per_second=global_per_minute / 60.0,
        )
        self._per_chat_capacity = per_chat_per_minute
        self._per_chat_refill_per_second = per_chat_per_minute / 60.0
        self._chat_buckets: dict[str, _TokenBucket] = {}

    def _get_chat_bucket(self, chat_id: str) -> _TokenBucket:
        bucket = self._chat_buckets.get(chat_id)
        if bucket is None:
            bucket = _TokenBucket(
                capacity=self._per_chat_capacity,
                refill_per_second=self._per_chat_refill_per_second,
            )
            self._chat_buckets[chat_id] = bucket
        return bucket

    def acquire(self, chat_id: str, *, now: float | None = None) -> RateLimitVerdict:
        if not chat_id:
            raise ValueError("chat_id cannot be empty")
        current = now if now is not None else monotonic()
        with self._lock:
            chat_bucket = self._get_chat_bucket(chat_id)
            # Check both first so we don't consume one and reject the other.
            if self._global.available(now=current) < 1.0:
                return RateLimitVerdict(allowed=False, blocked_by="global")
            if chat_bucket.available(now=current) < 1.0:
                return RateLimitVerdict(allowed=False, blocked_by="per_chat")
            assert self._global.try_acquire(now=current)
            assert chat_bucket.try_acquire(now=current)
            return RateLimitVerdict(allowed=True, blocked_by=None)

    def snapshot(self, *, now: float | None = None) -> dict[str, float]:
        current = now if now is not None else monotonic()
        with self._lock:
            snapshot = {"global": round(self._global.available(now=current), 3)}
            for chat_id, bucket in self._chat_buckets.items():
                snapshot[f"chat:{chat_id}"] = round(bucket.available(now=current), 3)
            return snapshot
