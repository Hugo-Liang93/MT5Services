from __future__ import annotations

from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace

from src.clients.mt5_market import Quote, Tick
from src.market.event_bus import QuoteEvent, TickBatchEvent, TickBatchEventBus
from src.market.service import MarketDataService


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        tick_cache_size=100,
        tick_limit=20,
        quote_stale_seconds=5,
        ohlc_event_queue_size=100,
        intrabar_max_points=10,
        default_symbol="EURUSD",
        ohlc_limit=100,
        ohlc_cache_limit=100,
    )


def _tick(time_msc: int, last: float = 1.1000) -> Tick:
    return Tick(
        symbol="EURUSD",
        bid=last - 0.0001,
        ask=last + 0.0001,
        last=last,
        volume=1.0,
        time=datetime.fromtimestamp(time_msc / 1000, tz=timezone.utc),
        time_msc=time_msc,
        flags=6,
    )


def _quote(time_msc: int, last: float = 1.1000) -> Quote:
    return Quote(
        symbol="EURUSD",
        bid=last - 0.0001,
        ask=last + 0.0001,
        last=last,
        volume=1.0,
        time=datetime.fromtimestamp(time_msc / 1000, tz=timezone.utc),
    )


def test_set_quote_dispatches_quote_event_without_writing_tick_cache() -> None:
    service = MarketDataService(
        client=SimpleNamespace(),
        market_settings=_settings(),
    )
    received: list[QuoteEvent] = []
    delivered = Event()

    def listener(event: QuoteEvent) -> None:
        received.append(event)
        delivered.set()

    service.add_quote_listener(listener)

    try:
        service.set_quote("EURUSD", _quote(1_767_225_600_000))

        assert delivered.wait(1.0)
        assert len(received) == 1
        event = received[0]
        assert event.symbol == "EURUSD"
        assert event.quote.bid == 1.0999
        assert service.get_ticks("EURUSD", limit=10) == []
    finally:
        service.shutdown()


def test_extend_ticks_dispatches_immutable_tick_batch_event() -> None:
    service = MarketDataService(
        client=SimpleNamespace(),
        market_settings=_settings(),
    )
    received: list[TickBatchEvent] = []
    delivered = Event()

    def listener(event: TickBatchEvent) -> None:
        received.append(event)
        delivered.set()

    service.add_tick_batch_listener(listener)

    try:
        service.extend_ticks(
            "EURUSD", [_tick(1_767_225_600_000), _tick(1_767_225_601_000)]
        )

        assert delivered.wait(1.0)
        assert len(received) == 1
        event = received[0]
        assert event.symbol == "EURUSD"
        assert isinstance(event.ticks, tuple)
        assert event.latest_time_msc == 1_767_225_601_000
        assert event.ticks[0].last == 1.1000
        assert service.get_ticks("EURUSD", limit=10) == list(event.ticks)
    finally:
        service.shutdown()


def test_remove_tick_batch_listener_stops_dispatch() -> None:
    service = MarketDataService(
        client=SimpleNamespace(),
        market_settings=_settings(),
    )
    received: list[TickBatchEvent] = []
    listener = received.append
    service.add_tick_batch_listener(listener)
    service.remove_tick_batch_listener(listener)

    try:
        service.extend_ticks("EURUSD", [_tick(1_767_225_600_000)])

        assert received == []
        assert service.tick_event_bus_stats()["listeners"] == 0
    finally:
        service.shutdown()


def test_tick_listener_exception_is_recorded_and_does_not_block_others() -> None:
    service = MarketDataService(
        client=SimpleNamespace(),
        market_settings=_settings(),
    )
    received: list[TickBatchEvent] = []

    def bad_listener(_event: TickBatchEvent) -> None:
        raise RuntimeError("listener failed")

    service.add_tick_batch_listener(bad_listener)
    delivered = Event()

    def good_listener(event: TickBatchEvent) -> None:
        received.append(event)
        delivered.set()

    service.add_tick_batch_listener(good_listener)

    try:
        service.extend_ticks("EURUSD", [_tick(1_767_225_600_000)])

        assert delivered.wait(1.0)
        stats = service.tick_event_bus_stats()
        assert len(received) == 1
        assert stats["failed_dispatches"] == 1
        assert stats["last_error"] == "listener failed"
    finally:
        service.shutdown()


def test_tick_batch_event_bus_deduplicates_listeners() -> None:
    bus = TickBatchEventBus()
    received: list[TickBatchEvent] = []
    delivered = Event()

    def listener(event: TickBatchEvent) -> None:
        received.append(event)
        delivered.set()

    try:
        bus.add_listener(listener)
        bus.add_listener(listener)
        bus.publish(
            TickBatchEvent(
                symbol="EURUSD",
                ticks=(_tick(1_767_225_600_000),),
                received_at=datetime.now(timezone.utc),
                latest_time_msc=1_767_225_600_000,
            )
        )

        assert delivered.wait(1.0)
        assert len(received) == 1
        assert bus.stats()["listeners"] == 1
    finally:
        bus.shutdown()


def test_market_service_health_includes_tick_event_bus_stats() -> None:
    service = MarketDataService(
        client=SimpleNamespace(health=lambda: {"status": "ok"}),
        market_settings=_settings(),
    )

    try:
        service.add_tick_batch_listener(lambda _event: None)

        health = service.health()

        assert health["tick_event_bus"]["listeners"] == 1
    finally:
        service.shutdown()
