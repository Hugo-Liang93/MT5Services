from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable, Optional

from .service import SignalModule

if TYPE_CHECKING:
    from src.clients.mt5_market import OHLC
    from src.core.market_service import MarketDataService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignalTarget:
    symbol: str
    timeframe: str
    strategy: str


class SignalRuntime:
    """Event-driven runtime based on close-bar and intrabar events."""

    def __init__(
        self,
        service: SignalModule,
        market_service: "MarketDataService",
        targets: Iterable[SignalTarget],
        enable_close_bar: bool = True,
        enable_intrabar: bool = False,
    ):
        self.service = service
        self.market_service = market_service
        self.enable_close_bar = bool(enable_close_bar)
        self.enable_intrabar = bool(enable_intrabar)
        self._targets = list(targets)
        self._target_index: dict[tuple[str, str], list[str]] = {}
        for target in self._targets:
            self._target_index.setdefault((target.symbol, target.timeframe), []).append(target.strategy)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._events: queue.Queue = queue.Queue(maxsize=4096)
        self._last_run_at: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._run_count = 0
        self._processed_events = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        if self.enable_close_bar:
            self.market_service.add_ohlc_close_listener(self._on_close_bar)
        if self.enable_intrabar:
            self.market_service.add_intrabar_listener(self._on_intrabar)
        self._thread = threading.Thread(target=self._loop, name="signal-runtime", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self.market_service.remove_ohlc_close_listener(self._on_close_bar)
        self.market_service.remove_intrabar_listener(self._on_intrabar)
        if self._thread:
            self._thread.join(timeout=timeout)

    def _on_close_bar(self, symbol: str, timeframe: str, _bar_time: datetime) -> None:
        self._enqueue(("close_bar", symbol, timeframe))

    def _on_intrabar(self, symbol: str, timeframe: str, _bar: "OHLC") -> None:
        self._enqueue(("intrabar", symbol, timeframe))

    def _enqueue(self, item: tuple[str, str, str]) -> None:
        try:
            self._events.put_nowait(item)
        except queue.Full:
            logger.warning("Signal runtime queue is full, dropping event: %s", item)

    def status(self) -> dict:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "target_count": len(self._targets),
            "trigger_mode": {
                "close_bar": self.enable_close_bar,
                "intrabar": self.enable_intrabar,
            },
            "run_count": self._run_count,
            "processed_events": self._processed_events,
            "queue_size": self._events.qsize(),
            "last_run_at": self._last_run_at.isoformat() if self._last_run_at else None,
            "last_error": self._last_error,
        }


    def process_next_event(self, timeout: float = 0.5) -> bool:
        try:
            event = self._events.get(timeout=timeout)
        except queue.Empty:
            return False
        _, symbol, timeframe = event
        strategies = self._target_index.get((symbol, timeframe), [])
        for strategy in strategies:
            self.service.evaluate(
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy,
                persist=True,
            )
        self._processed_events += 1
        self._run_count += 1
        self._last_run_at = datetime.now(timezone.utc)
        self._last_error = None
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.process_next_event(timeout=0.5)
            except Exception as exc:
                self._last_error = str(exc)
                logger.exception("Signal runtime event processing failed: %s", exc)
