"""PipelineEventBus — lightweight broadcast bus for pipeline trace events.

Each processing stage (bar close → indicator compute → snapshot publish → signal
evaluate) emits a typed event carrying a shared ``trace_id``.  SSE consumers
subscribe via :meth:`add_listener` and receive real-time pipeline flow data for
frontend visualisation.

Design constraints:
- **Read-only observer** — never modifies the data path.
- **Best-effort** — listener failures or slow consumers do not block producers.
- **Thread-safe** — producers call from different background threads.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Event types ─────────────────────────────────────────────────

PIPELINE_BAR_CLOSED = "bar_closed"
PIPELINE_INDICATOR_COMPUTED = "indicator_computed"
PIPELINE_SNAPSHOT_PUBLISHED = "snapshot_published"
PIPELINE_SIGNAL_EVALUATED = "signal_evaluated"


@dataclass(frozen=True)
class PipelineEvent:
    """Immutable event emitted at each pipeline stage."""

    type: str
    trace_id: str
    symbol: str
    timeframe: str
    scope: str  # "confirmed" | "intrabar"
    ts: str  # ISO-8601 UTC timestamp of this event
    # Stage-specific payload (indicator count, compute time, signal state …)
    payload: Dict[str, Any] = field(default_factory=dict)


PipelineListener = Callable[[PipelineEvent], None]


# ── Bus ─────────────────────────────────────────────────────────


class PipelineEventBus:
    """Thread-safe pub/sub bus for pipeline trace events.

    Producers call :meth:`emit` from any thread; listeners are invoked
    synchronously but wrapped in ``try/except`` so a failing listener
    never disrupts the pipeline.
    """

    def __init__(self, *, max_listeners: int = 64) -> None:
        self._lock = threading.Lock()
        self._listeners: List[PipelineListener] = []
        self._max_listeners = max_listeners
        self._shutdown = False

        # Lightweight counters for admin / health
        self._total_emitted: int = 0
        self._total_listener_errors: int = 0

    # ── Listener management ─────────────────────────────────────

    def add_listener(self, listener: PipelineListener) -> bool:
        """Register a listener.  Returns False if limit reached."""
        with self._lock:
            if len(self._listeners) >= self._max_listeners:
                logger.warning(
                    "PipelineEventBus: listener limit (%d) reached, rejecting",
                    self._max_listeners,
                )
                return False
            self._listeners.append(listener)
            return True

    def remove_listener(self, listener: PipelineListener) -> None:
        with self._lock:
            self._listeners = [fn for fn in self._listeners if fn is not listener]

    # ── Emit ────────────────────────────────────────────────────

    def emit(self, event: PipelineEvent) -> None:
        """Broadcast *event* to all registered listeners (best-effort)."""
        if self._shutdown:
            return
        with self._lock:
            targets = list(self._listeners)
        self._total_emitted += 1
        for fn in targets:
            try:
                fn(event)
            except Exception:
                self._total_listener_errors += 1
                logger.debug(
                    "PipelineEventBus: listener error for %s/%s trace=%s",
                    event.symbol,
                    event.timeframe,
                    event.trace_id,
                    exc_info=True,
                )

    # ── Convenience helpers ─────────────────────────────────────

    def emit_bar_closed(
        self,
        trace_id: str,
        symbol: str,
        timeframe: str,
        scope: str,
        bar_time: datetime,
        *,
        ohlc: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {"bar_time": bar_time.isoformat()}
        if ohlc:
            payload["ohlc"] = ohlc
        self.emit(
            PipelineEvent(
                type=PIPELINE_BAR_CLOSED,
                trace_id=trace_id,
                symbol=symbol,
                timeframe=timeframe,
                scope=scope,
                ts=datetime.now(timezone.utc).isoformat(),
                payload=payload,
            )
        )

    def emit_indicator_computed(
        self,
        trace_id: str,
        symbol: str,
        timeframe: str,
        scope: str,
        compute_time_ms: float,
        indicator_count: int,
        *,
        indicator_names: Optional[List[str]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "compute_time_ms": round(compute_time_ms, 2),
            "indicator_count": indicator_count,
        }
        if indicator_names:
            payload["indicator_names"] = indicator_names
        self.emit(
            PipelineEvent(
                type=PIPELINE_INDICATOR_COMPUTED,
                trace_id=trace_id,
                symbol=symbol,
                timeframe=timeframe,
                scope=scope,
                ts=datetime.now(timezone.utc).isoformat(),
                payload=payload,
            )
        )

    def emit_snapshot_published(
        self,
        trace_id: str,
        symbol: str,
        timeframe: str,
        scope: str,
        indicator_count: int,
        bar_time: datetime,
        *,
        indicators: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "indicator_count": indicator_count,
            "bar_time": bar_time.isoformat(),
        }
        if indicators is not None:
            payload["indicators"] = indicators
        self.emit(
            PipelineEvent(
                type=PIPELINE_SNAPSHOT_PUBLISHED,
                trace_id=trace_id,
                symbol=symbol,
                timeframe=timeframe,
                scope=scope,
                ts=datetime.now(timezone.utc).isoformat(),
                payload=payload,
            )
        )

    def emit_signal_evaluated(
        self,
        trace_id: str,
        symbol: str,
        timeframe: str,
        scope: str,
        strategy: str,
        direction: str,
        confidence: float,
        signal_state: str,
    ) -> None:
        self.emit(
            PipelineEvent(
                type=PIPELINE_SIGNAL_EVALUATED,
                trace_id=trace_id,
                symbol=symbol,
                timeframe=timeframe,
                scope=scope,
                ts=datetime.now(timezone.utc).isoformat(),
                payload={
                    "strategy": strategy,
                    "direction": direction,
                    "confidence": round(confidence, 4),
                    "signal_state": signal_state,
                },
            )
        )

    # ── Status / lifecycle ──────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            listener_count = len(self._listeners)
        return {
            "listeners": listener_count,
            "total_emitted": self._total_emitted,
            "total_listener_errors": self._total_listener_errors,
        }

    def shutdown(self) -> None:
        self._shutdown = True
        with self._lock:
            self._listeners.clear()
