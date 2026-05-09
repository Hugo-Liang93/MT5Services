from __future__ import annotations

from collections import deque
from datetime import datetime

from src.clients.mt5_market import Tick
from src.market.event_bus import QuoteEvent, TickBatchEvent
from src.market.tick_features.bus import TickFeatureBus
from src.market.tick_features.calculator import TickFeatureCalculator
from src.market.tick_features.health import TickFeatureHealthStore
from src.market.tick_features.models import TickFeatureConfig


class TickFeatureEngine:
    """Consumes tick batch events and emits tick-derived feature snapshots."""

    def __init__(
        self,
        calculator: TickFeatureCalculator,
        feature_bus: TickFeatureBus,
        health_store: TickFeatureHealthStore,
        config: TickFeatureConfig,
    ) -> None:
        self._calculator = calculator
        self._feature_bus = feature_bus
        self._health_store = health_store
        self._config = config
        self._buffers: dict[str, deque[Tick]] = {}
        self._last_emit_msc: dict[str, int] = {}

    def on_tick_batch(self, event: TickBatchEvent) -> None:
        if not event.ticks:
            return
        self._ingest_ticks(
            symbol=event.symbol,
            ticks=event.ticks,
            received_at=event.received_at,
        )

    def on_quote(self, event: QuoteEvent) -> None:
        quote = event.quote
        time_msc = int(quote.time.timestamp() * 1000)
        tick = Tick(
            symbol=quote.symbol,
            bid=quote.bid,
            ask=quote.ask,
            last=quote.last,
            volume=quote.volume,
            time=quote.time,
            time_msc=time_msc,
            flags=0,
        )
        self._ingest_ticks(
            symbol=event.symbol,
            ticks=(tick,),
            received_at=event.received_at,
        )

    def _ingest_ticks(
        self,
        *,
        symbol: str,
        ticks: tuple[Tick, ...],
        received_at: datetime,
    ) -> None:
        buffer = self._buffers.setdefault(symbol, deque())
        for tick in ticks:
            buffer.append(tick)
        latest_msc = max(
            (int(tick.time_msc) for tick in ticks if tick.time_msc is not None),
            default=None,
        )
        if latest_msc is None:
            return

        cutoff_msc = latest_msc - int(self._config.window_seconds * 1000)
        while buffer and buffer[0].time_msc is not None and int(buffer[0].time_msc) < cutoff_msc:
            buffer.popleft()

        last_emit_msc = self._last_emit_msc.get(symbol)
        emit_delta_msc = int(self._config.emit_interval_seconds * 1000)
        if last_emit_msc is not None and latest_msc - last_emit_msc < emit_delta_msc:
            return

        snapshot = self._calculator.calculate(
            symbol=symbol,
            ticks=tuple(buffer),
            now=received_at,
        )
        self._feature_bus.publish(snapshot)
        self._health_store.update_from_snapshot(snapshot, self._feature_bus.stats())
        self._last_emit_msc[symbol] = latest_msc
