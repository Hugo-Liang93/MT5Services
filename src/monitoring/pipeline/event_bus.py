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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .events import (
    PIPELINE_BAR_CLOSED,
    PIPELINE_EXECUTION_BLOCKED,
    PIPELINE_EXECUTION_DECIDED,
    PIPELINE_EXECUTION_FAILED,
    PIPELINE_EXECUTION_SKIPPED,
    PIPELINE_EXECUTION_SUBMITTED,
    PIPELINE_INDICATOR_COMPUTED,
    PIPELINE_PENDING_ORDER_SUBMITTED,
    PIPELINE_SIGNAL_EVALUATED,
    PIPELINE_SIGNAL_FILTER_DECIDED,
    PIPELINE_SNAPSHOT_PUBLISHED,
    PIPELINE_VOTING_COMPLETED,
)

logger = logging.getLogger(__name__)


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

    def __init__(
        self,
        *,
        max_listeners: int = 64,
        queue_size: int = 4096,
    ) -> None:
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
        """Broadcast *event* to all registered listeners synchronously.

        每个 listener 包裹在 try/except 中，单个失败不影响其他 listener 和生产者。
        """
        if self._shutdown:
            return
        self._total_emitted += 1
        with self._lock:
            targets = list(self._listeners)
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
        **extra: Any,
    ) -> None:
        data: Dict[str, Any] = {
            "strategy": strategy,
            "direction": direction,
            "confidence": round(confidence, 4),
            "signal_state": signal_state,
        }
        data.update(extra)
        self.emit(
            PipelineEvent(
                type=PIPELINE_SIGNAL_EVALUATED,
                trace_id=trace_id,
                symbol=symbol,
                timeframe=timeframe,
                scope=scope,
                ts=datetime.now(timezone.utc).isoformat(),
                payload=data,
            )
        )

    def emit_signal_filter_decided(
        self,
        trace_id: str,
        symbol: str,
        timeframe: str,
        scope: str,
        *,
        allowed: bool,
        reason: str,
        category: str,
        spread_points: float,
        active_sessions: list[str] | None = None,
        evaluation_time: datetime | None = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "allowed": bool(allowed),
            "reason": reason,
            "category": category,
            "spread_points": round(float(spread_points), 4),
            "active_sessions": list(active_sessions or []),
        }
        if evaluation_time is not None:
            if evaluation_time.tzinfo is None:
                evaluation_time = evaluation_time.replace(tzinfo=timezone.utc)
            else:
                evaluation_time = evaluation_time.astimezone(timezone.utc)
            payload["evaluation_time"] = evaluation_time.isoformat()
        self.emit(
            PipelineEvent(
                type=PIPELINE_SIGNAL_FILTER_DECIDED,
                trace_id=trace_id,
                symbol=symbol,
                timeframe=timeframe,
                scope=scope,
                ts=datetime.now(timezone.utc).isoformat(),
                payload=payload,
            )
        )

    def emit_execution_decided(
        self,
        trace_id: str,
        symbol: str,
        timeframe: str,
        scope: str,
        *,
        strategy: str,
        direction: str,
        order_kind: str,
    ) -> None:
        self.emit(
            PipelineEvent(
                type=PIPELINE_EXECUTION_DECIDED,
                trace_id=trace_id,
                symbol=symbol,
                timeframe=timeframe,
                scope=scope,
                ts=datetime.now(timezone.utc).isoformat(),
                payload={
                    "strategy": strategy,
                    "direction": direction,
                    "order_kind": order_kind,
                },
            )
        )

    def emit_execution_blocked(
        self,
        trace_id: str,
        symbol: str,
        timeframe: str,
        scope: str,
        *,
        strategy: str,
        direction: str,
        reason: str,
        category: str,
    ) -> None:
        self.emit(
            PipelineEvent(
                type=PIPELINE_EXECUTION_BLOCKED,
                trace_id=trace_id,
                symbol=symbol,
                timeframe=timeframe,
                scope=scope,
                ts=datetime.now(timezone.utc).isoformat(),
                payload={
                    "strategy": strategy,
                    "direction": direction,
                    "reason": reason,
                    "category": category,
                },
            )
        )

    def emit_execution_submitted(
        self,
        trace_id: str,
        symbol: str,
        timeframe: str,
        scope: str,
        *,
        strategy: str,
        direction: str,
        order_kind: str,
        request_id: str,
        ticket: int | None = None,
    ) -> None:
        self.emit(
            PipelineEvent(
                type=PIPELINE_EXECUTION_SUBMITTED,
                trace_id=trace_id,
                symbol=symbol,
                timeframe=timeframe,
                scope=scope,
                ts=datetime.now(timezone.utc).isoformat(),
                payload={
                    "strategy": strategy,
                    "direction": direction,
                    "order_kind": order_kind,
                    "request_id": request_id,
                    "ticket": ticket,
                },
            )
        )

    def emit_pending_order_submitted(
        self,
        trace_id: str,
        symbol: str,
        timeframe: str,
        scope: str,
        *,
        strategy: str,
        direction: str,
        order_kind: str,
        request_id: str,
        ticket: int | None = None,
    ) -> None:
        self.emit(
            PipelineEvent(
                type=PIPELINE_PENDING_ORDER_SUBMITTED,
                trace_id=trace_id,
                symbol=symbol,
                timeframe=timeframe,
                scope=scope,
                ts=datetime.now(timezone.utc).isoformat(),
                payload={
                    "strategy": strategy,
                    "direction": direction,
                    "order_kind": order_kind,
                    "request_id": request_id,
                    "ticket": ticket,
                },
            )
        )

    def emit_execution_failed(
        self,
        trace_id: str,
        symbol: str,
        timeframe: str,
        scope: str,
        *,
        strategy: str,
        direction: str,
        order_kind: str,
        reason: str,
        category: str,
    ) -> None:
        self.emit(
            PipelineEvent(
                type=PIPELINE_EXECUTION_FAILED,
                trace_id=trace_id,
                symbol=symbol,
                timeframe=timeframe,
                scope=scope,
                ts=datetime.now(timezone.utc).isoformat(),
                payload={
                    "strategy": strategy,
                    "direction": direction,
                    "order_kind": order_kind,
                    "reason": reason,
                    "category": category,
                },
            )
        )

    def emit_voting_completed(
        self,
        trace_id: str,
        symbol: str,
        timeframe: str,
        scope: str,
        *,
        group_name: str,
        winning_direction: Optional[str],
        final_confidence: Optional[float],
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        data = dict(payload or {})
        data.update(
            {
                "group_name": group_name,
                "winning_direction": winning_direction,
                "final_confidence": final_confidence,
            }
        )
        self.emit(
            PipelineEvent(
                type=PIPELINE_VOTING_COMPLETED,
                trace_id=trace_id,
                symbol=symbol,
                timeframe=timeframe,
                scope=scope,
                ts=datetime.now(timezone.utc).isoformat(),
                payload=data,
            )
        )

    def emit_execution_skipped(
        self,
        trace_id: str,
        symbol: str,
        timeframe: str,
        scope: str,
        *,
        strategy: str,
        direction: str,
        skip_reason: str,
        skip_category: str,
        confidence: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        data = dict(extra or {})
        data.update(
            {
                "strategy": strategy,
                "direction": direction,
                "skip_reason": skip_reason,
                "skip_category": skip_category,
                "confidence": confidence,
            }
        )
        self.emit(
            PipelineEvent(
                type=PIPELINE_EXECUTION_SKIPPED,
                trace_id=trace_id,
                symbol=symbol,
                timeframe=timeframe,
                scope=scope,
                ts=datetime.now(timezone.utc).isoformat(),
                payload=data,
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
