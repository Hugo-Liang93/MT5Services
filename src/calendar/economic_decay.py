"""Calendar-domain economic event decay query port.

Single entry point for signal/risk/backtest domains to ask:
    "Given symbol S and evaluation time T, what's the confidence
     decay factor in [0.0, 1.0] caused by nearby high-impact events?"

This module owns:
- provider query parameter assembly
- symbol -> countries/currencies inference
- per-symbol short-TTL cache (60s default, keyed by at_time)
- exception layering contract (see decay_factor docstring)

Callers MUST pass an explicit ``at_time`` so this service stays
deterministic under backtest/replay (no implicit wall-clock reads).
The cache freshness window is measured against ``at_time`` itself,
not real wall clock, so backtest replay sees identical cache behaviour
regardless of how fast (or slow) the replay runs.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Optional, Protocol, TypedDict

from src.calendar.policy import SignalEconomicPolicy

logger = logging.getLogger(__name__)

_SIGNAL_EVENT_STATUSES = (
    "scheduled",
    "imminent",
    "pending_release",
    "released",
)


class EconomicEventsProvider(Protocol):
    def get_events(
        self,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int = 1000,
        sources: Optional[list[str]] = None,
        countries: Optional[list[str]] = None,
        currencies: Optional[list[str]] = None,
        session_buckets: Optional[list[str]] = None,
        statuses: Optional[list[str]] = None,
        importance_min: Optional[int] = None,
    ) -> list[Any]: ...


_RUNTIME_DEGRADE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    OSError,
    RuntimeError,
)


def _symbol_context(symbol: str) -> dict[str, list[str]]:
    from src.calendar import infer_symbol_context

    return infer_symbol_context(symbol)


class _CacheEntry(TypedDict):
    decay: float
    at_time: datetime


class EconomicDecayService:
    """Single port for economic event decay queries.

    Thread-safe; cache uses an internal lock.

    Event selection contract (locked by tests):
    - decay_factor picks the event with smallest |delta_minutes|
      from at_time, then applies policy.decay_for_delta to that delta.
    - Cache freshness is measured against the caller-supplied at_time
      (NOT wall clock). Entry is valid while
      ``|at_time - cached_at_time| < cache_ttl_seconds``; otherwise it
      is treated as expired and refetched.
    - Cache eviction (when over capacity) selects victims by oldest
      cached_at_time first.
    - Cache is best-effort, not single-flight: concurrent misses for the
      same symbol may issue duplicate provider queries; last writer wins.
    - Cache freshness uses ``|at_time - cached_at_time|`` (bidirectional).
      Callers SHOULD pass monotonic at_time per symbol (the typical live /
      sequential backtest case). Out-of-order calls within ttl may receive
      a decay computed against a different at_time's event window —
      bounded by ttl but technically stale.
    """

    def __init__(
        self,
        provider: EconomicEventsProvider,
        policy: SignalEconomicPolicy,
        *,
        cache_ttl_seconds: float = 60.0,
        cache_max_entries: int = 128,
    ) -> None:
        self._provider = provider
        self._policy = policy
        self._cache_ttl = float(cache_ttl_seconds)
        self._cache_max = int(cache_max_entries)
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    def decay_factor(self, symbol: str, *, at_time: datetime) -> float:
        """Return decay in [0.0, 1.0]; 1.0 means no influence.

        Exception contract (enforced by tests):
        - policy.enabled=False / provider missing -> 1.0 (designed degrade, no log)
        - provider raises ConnectionError/TimeoutError/OSError/RuntimeError
              -> 1.0 + WARNING log; result NOT cached
        - provider raises NameError/AttributeError/ImportError/TypeError/...
              -> propagated (fail-fast, surfaces coding bugs immediately)
        """
        if not self._policy.enabled or self._provider is None:
            return 1.0

        cached = self._read_cache(symbol, at_time)
        if cached is not None:
            return cached

        try:
            decay = self._query(symbol, at_time)
        except _RUNTIME_DEGRADE_EXCEPTIONS as exc:
            logger.warning(
                "EconomicDecayService runtime degrade for %s: %s: %s",
                symbol,
                type(exc).__name__,
                exc,
            )
            return 1.0

        self._write_cache(symbol, decay, at_time)
        return decay

    def has_blocking_event(self, symbol: str, *, at_time: datetime) -> tuple[bool, str]:
        """Hard-block check using filter_window; returns (blocked, reason)."""
        if not self._policy.enabled or self._provider is None:
            return False, ""
        try:
            context = _symbol_context(symbol)
            events = self._provider.get_events(
                start_time=at_time
                - timedelta(minutes=self._policy.filter_window.lookback_minutes),
                end_time=at_time
                + timedelta(minutes=self._policy.filter_window.lookahead_minutes),
                limit=50,
                countries=context["countries"] or None,
                currencies=context["currencies"] or None,
                statuses=list(_SIGNAL_EVENT_STATUSES),
                importance_min=self._policy.filter_window.importance_min,
            )
        except _RUNTIME_DEGRADE_EXCEPTIONS as exc:
            logger.warning(
                "EconomicDecayService.has_blocking_event runtime degrade"
                " for %s: %s: %s",
                symbol,
                type(exc).__name__,
                exc,
            )
            return False, ""
        if events:
            return True, "economic_event_block"
        return False, ""

    def _query(self, symbol: str, at_time: datetime) -> float:
        context = _symbol_context(symbol)
        events = self._provider.get_events(
            start_time=at_time
            - timedelta(minutes=self._policy.query_window.lookback_minutes),
            end_time=at_time
            + timedelta(minutes=self._policy.query_window.lookahead_minutes),
            limit=5,
            countries=context["countries"] or None,
            currencies=context["currencies"] or None,
            statuses=list(_SIGNAL_EVENT_STATUSES),
            importance_min=self._policy.query_window.importance_min,
        )
        if not events:
            return 1.0

        best_delta: float | None = None
        for evt in events:
            event_time = getattr(evt, "event_time", None)
            if event_time is None:
                continue
            delta = (event_time - at_time).total_seconds() / 60.0
            if best_delta is None or abs(delta) < abs(best_delta):
                best_delta = delta
        if best_delta is None:
            return 1.0
        return self._policy.decay_for_delta(best_delta)

    def _read_cache(self, symbol: str, at_time: datetime) -> float | None:
        with self._lock:
            entry = self._cache.get(symbol)
            if entry is None:
                return None
            cached_at: datetime = entry["at_time"]
            age_seconds = abs((at_time - cached_at).total_seconds())
            if age_seconds < self._cache_ttl:
                return entry["decay"]
            return None

    def _write_cache(self, symbol: str, decay: float, at_time: datetime) -> None:
        with self._lock:
            self._cache[symbol] = _CacheEntry(decay=decay, at_time=at_time)
            if len(self._cache) > self._cache_max:
                # Evict expired entries first (relative to incoming at_time).
                expired = [
                    k
                    for k, v in self._cache.items()
                    if abs((at_time - v["at_time"]).total_seconds()) >= self._cache_ttl
                ]
                for k in expired:
                    del self._cache[k]
                if len(self._cache) > self._cache_max:
                    # Then evict oldest cached_at_time (FIFO under uniform TTL).
                    by_age = sorted(
                        self._cache.items(), key=lambda kv: kv[1]["at_time"]
                    )
                    for k, _ in by_age[: len(self._cache) - self._cache_max]:
                        del self._cache[k]
