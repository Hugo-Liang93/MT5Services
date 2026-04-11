"""Intrabar queue helpers for UnifiedIndicatorManager."""

from __future__ import annotations

import logging
import queue
import time
from typing import Any

from .intrabar_metrics import record_intrabar_drop

from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class IntrabarQueueItem:
    symbol: str
    timeframe: str
    bar: Any
    enqueued_at_monotonic: float


def enqueue_intrabar_event(
    manager,
    symbol: str,
    timeframe: str,
    bar: Any,
) -> None:
    """Enqueue intrabar event without blocking the ingestor thread."""
    try:
        item = IntrabarQueueItem(
            symbol=symbol,
            timeframe=timeframe,
            bar=bar,
            enqueued_at_monotonic=time.monotonic(),
        )
        manager.state.intrabar_queue.put_nowait(item)
    except queue.Full:
        record_intrabar_drop(manager)
        logger.warning(
            "Intrabar indicator queue full, dropped %s/%s at %s",
            symbol,
            timeframe,
            getattr(bar, "time", "?"),
        )
