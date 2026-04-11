"""Event I/O helpers for UnifiedIndicatorManager."""

from __future__ import annotations

import logging
import queue

if False:  # pragma: no cover
    from ..manager import UnifiedIndicatorManager

logger = logging.getLogger(__name__)


def flush_event_batch(
    manager: "UnifiedIndicatorManager",
    first_item: object,
    phase: str = "batch",
) -> None:
    """Write *first_item* plus up to 63 more queued items to SQLite."""
    batch: list = [first_item]
    for _ in range(63):
        try:
            batch.append(manager.state.event_write_queue.get_nowait())
        except queue.Empty:
            break
    try:
        manager.event_store.publish_events_batch(batch)
    except Exception:
        logger.exception(
            "Failed to persist %s queued bar close events (%s)",
            len(batch),
            phase,
        )


def publish_closed_bar_event(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    bar_time,
) -> None:
    """Non-blocking: enqueue for async SQLite write by event writer loop."""
    try:
        manager.state.event_write_queue.put_nowait((symbol, timeframe, bar_time))
    except queue.Full:
        logger.warning(
            "Event write queue full — bar close event dropped for %s/%s at %s; "
            "reconcile will recover it.",
            symbol,
            timeframe,
            bar_time,
        )
