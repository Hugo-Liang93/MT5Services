"""MarketEventBus — listener management and event dispatch for market data.

Extracted from MarketDataService to separate event-driven concerns
from cache management. MarketDataService delegates to this class.
"""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import time
import threading
from datetime import datetime
from typing import Any, Callable, List, Optional

from src.utils.common import same_listener_reference

logger = logging.getLogger(__name__)


class MarketEventBus:
    """Manages OHLC close and intrabar event subscriptions and dispatch.

    Thread-safe: all listener mutations are guarded by an internal lock.
    Callbacks are dispatched asynchronously via a ThreadPoolExecutor to
    prevent slow listeners from blocking the ingestor thread.
    """

    def __init__(self, *, ohlc_event_queue_size: int = 1000) -> None:
        self._lock = threading.Lock()
        self._shutdown_flag = False
        self._ohlc_close_listeners: List[Callable[[str, str, datetime], None]] = []
        self._intrabar_listeners: List[Callable[[str, str, Any], None]] = []
        self._ohlc_event_sink: Optional[Callable[[str, str, datetime], None]] = None
        self._ohlc_event_queue: queue.Queue[tuple] = queue.Queue(
            maxsize=ohlc_event_queue_size,
        )
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="mds-listener",
        )

    # ── OHLC close listeners ──────────────────────────────────

    def add_ohlc_close_listener(
        self, listener: Callable[[str, str, datetime], None],
    ) -> None:
        with self._lock:
            self._ohlc_close_listeners.append(listener)

    def remove_ohlc_close_listener(
        self, listener: Callable[[str, str, datetime], None],
    ) -> None:
        with self._lock:
            self._ohlc_close_listeners = [
                item
                for item in self._ohlc_close_listeners
                if not same_listener_reference(item, listener)
            ]

    # ── Intrabar listeners ────────────────────────────────────

    def add_intrabar_listener(
        self, listener: Callable[[str, str, Any], None],
    ) -> None:
        with self._lock:
            self._intrabar_listeners.append(listener)

    def remove_intrabar_listener(
        self, listener: Callable[[str, str, Any], None],
    ) -> None:
        with self._lock:
            self._intrabar_listeners = [
                item
                for item in self._intrabar_listeners
                if not same_listener_reference(item, listener)
            ]

    # ── Durable event sink / queue ────────────────────────────

    def set_ohlc_event_sink(
        self, sink: Optional[Callable[[str, str, datetime], None]],
    ) -> None:
        """Register a durable event sink for closed-bar notifications."""
        self._ohlc_event_sink = sink

    def get_ohlc_event(
        self, timeout: Optional[float] = None,
    ) -> Optional[tuple]:
        try:
            return self._ohlc_event_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    # ── Dispatch ──────────────────────────────────────────────

    def dispatch_ohlc_closed(
        self, symbol: str, timeframe: str, bar_time: datetime,
    ) -> None:
        """Broadcast an OHLC close event to all listeners + sink/queue."""
        if self._shutdown_flag:
            return
        for listener in list(self._ohlc_close_listeners):
            try:
                self._executor.submit(
                    self._safe_call_ohlc_listener, listener, symbol, timeframe, bar_time,
                )
            except RuntimeError:
                return  # executor already shut down
        if self._ohlc_event_sink is not None:
            try:
                self._ohlc_event_sink(symbol, timeframe, bar_time)
                return
            except Exception:
                logger.exception(
                    "Failed to publish OHLC close event for %s/%s at %s",
                    symbol, timeframe, bar_time,
                )
        try:
            self._ohlc_event_queue.put_nowait((symbol, timeframe, bar_time))
        except queue.Full:
            logger.warning(
                "Dropped in-memory OHLC close event because the queue is full: %s/%s %s",
                symbol, timeframe, bar_time,
            )

    def dispatch_intrabar(
        self, symbol: str, timeframe: str, bar: Any,
    ) -> None:
        """Broadcast an intrabar update to all listeners."""
        if self._shutdown_flag:
            return
        for listener in list(self._intrabar_listeners):
            try:
                self._executor.submit(
                    self._safe_call_intrabar_listener, listener, symbol, timeframe, bar,
                )
            except RuntimeError:
                return  # executor already shut down

    # ── Lifecycle ─────────────────────────────────────────────

    def shutdown(self) -> None:
        self._shutdown_flag = True
        self._executor.shutdown(wait=True, cancel_futures=False)
        logger.info("MarketEventBus: listener executor shutdown complete")

    # ── Safe call wrappers ────────────────────────────────────

    @staticmethod
    def _safe_call_ohlc_listener(
        listener: Callable[[str, str, datetime], None],
        symbol: str,
        timeframe: str,
        bar_time: datetime,
    ) -> None:
        try:
            listener(symbol, timeframe, bar_time)
        except Exception:
            logger.exception(
                "Failed to notify OHLC close listener for %s/%s at %s",
                symbol, timeframe, bar_time,
            )

    @staticmethod
    def _safe_call_intrabar_listener(
        listener: Callable[[str, str, Any], None],
        symbol: str,
        timeframe: str,
        bar: Any,
    ) -> None:
        try:
            t0 = time.monotonic()
            listener(symbol, timeframe, bar)
            elapsed_ms = (time.monotonic() - t0) * 1000
            if elapsed_ms > 100:
                logger.warning(
                    "Slow intrabar listener for %s/%s took %.1fms",
                    symbol, timeframe, elapsed_ms,
                )
        except Exception:
            logger.exception(
                "Failed to publish intrabar event for %s/%s at %s",
                symbol, timeframe, getattr(bar, "time", "?"),
            )
