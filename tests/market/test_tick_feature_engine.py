from __future__ import annotations

from datetime import datetime, timezone

from src.clients.mt5_market import Quote, Tick
from src.market.event_bus import QuoteEvent, TickBatchEvent
from src.market.tick_features.bus import TickFeatureBus
from src.market.tick_features.calculator import TickFeatureCalculator
from src.market.tick_features.engine import TickFeatureEngine
from src.market.tick_features.health import TickFeatureHealthStore
from src.market.tick_features.models import TickFeatureConfig


def _tick(time_msc: int, last: float) -> Tick:
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


def _quote(time_msc: int, last: float) -> Quote:
    return Quote(
        symbol="EURUSD",
        bid=last - 0.0001,
        ask=last + 0.0001,
        last=last,
        volume=1.0,
        time=datetime.fromtimestamp(time_msc / 1000, tz=timezone.utc),
    )


def _event(*ticks: Tick) -> TickBatchEvent:
    return TickBatchEvent(
        symbol="EURUSD",
        ticks=tuple(ticks),
        received_at=datetime.fromtimestamp(
            max(tick.time_msc or 0 for tick in ticks) / 1000,
            tz=timezone.utc,
        ),
        latest_time_msc=max(tick.time_msc or 0 for tick in ticks),
    )


def _quote_event(time_msc: int, last: float) -> QuoteEvent:
    return QuoteEvent(
        symbol="EURUSD",
        quote=_quote(time_msc, last),
        received_at=datetime.fromtimestamp(time_msc / 1000, tz=timezone.utc),
    )


def test_engine_can_build_features_from_realtime_quote_events() -> None:
    config = TickFeatureConfig(
        window_seconds=5.0,
        emit_interval_seconds=0.1,
        min_ticks_per_window=3,
        point_size=0.0001,
    )
    bus = TickFeatureBus(maxlen=10)
    health_store = TickFeatureHealthStore(max_snapshot_age_seconds=3)
    engine = TickFeatureEngine(TickFeatureCalculator(config), bus, health_store, config)

    engine.on_quote(_quote_event(1_000, 1.1000))
    engine.on_quote(_quote_event(1_500, 1.1001))
    engine.on_quote(_quote_event(2_200, 1.1002))

    latest = bus.drain(10)[-1]
    assert latest.status == "healthy"
    assert latest.tick_count == 3
    assert latest.window_end_msc == 2_200
    assert latest.bid == 1.1001
    assert latest.ask == 1.1003
    assert latest.quote_age_ms == 0


def test_engine_uses_symbol_point_size_for_spread_gate() -> None:
    config = TickFeatureConfig(
        window_seconds=5.0,
        emit_interval_seconds=0.1,
        min_ticks_per_window=3,
        point_size=0.00001,
        max_spread_points=30.0,
        point_size_by_symbol={"XAUUSD": 0.01},
        max_spread_points_by_symbol={"XAUUSD": 80.0},
    )
    bus = TickFeatureBus(maxlen=10)
    health_store = TickFeatureHealthStore(max_snapshot_age_seconds=3)
    engine = TickFeatureEngine(TickFeatureCalculator(config), bus, health_store, config)

    for time_msc, last in ((1_000, 3500.00), (1_500, 3500.01), (2_200, 3500.02)):
        engine.on_quote(
            QuoteEvent(
                symbol="XAUUSD",
                quote=Quote(
                    symbol="XAUUSD",
                    bid=last - 0.10,
                    ask=last + 0.10,
                    last=last,
                    volume=1.0,
                    time=datetime.fromtimestamp(time_msc / 1000, tz=timezone.utc),
                ),
                received_at=datetime.fromtimestamp(time_msc / 1000, tz=timezone.utc),
            )
        )

    latest = bus.drain(10)[-1]
    assert latest.status == "healthy"
    assert latest.spread_points == 20.0


def test_engine_emits_only_after_configured_interval() -> None:
    config = TickFeatureConfig(
        emit_interval_seconds=1.0,
        min_ticks_per_window=1,
        point_size=0.0001,
    )
    bus = TickFeatureBus(maxlen=10)
    health_store = TickFeatureHealthStore(max_snapshot_age_seconds=3)
    engine = TickFeatureEngine(TickFeatureCalculator(config), bus, health_store, config)

    engine.on_tick_batch(_event(_tick(1_000, 1.1000)))
    engine.on_tick_batch(_event(_tick(1_500, 1.1001)))
    engine.on_tick_batch(_event(_tick(2_100, 1.1002)))

    snapshots = bus.drain(10)
    assert [snapshot.window_end_msc for snapshot in snapshots] == [1_000, 2_100]


def test_engine_keeps_per_symbol_window_bounded() -> None:
    config = TickFeatureConfig(
        window_seconds=1.0,
        emit_interval_seconds=0.1,
        min_ticks_per_window=1,
        point_size=0.0001,
    )
    bus = TickFeatureBus(maxlen=10)
    health_store = TickFeatureHealthStore(max_snapshot_age_seconds=3)
    engine = TickFeatureEngine(TickFeatureCalculator(config), bus, health_store, config)

    engine.on_tick_batch(_event(_tick(1_000, 1.1000), _tick(1_500, 1.1001)))
    engine.on_tick_batch(_event(_tick(2_600, 1.1002)))

    latest = bus.drain(10)[-1]
    assert latest.window_start_msc == 1_600
    assert latest.tick_count == 1
    assert latest.window_end_msc == 2_600


def test_feature_bus_reports_backlog_and_drops_oldest_when_full() -> None:
    config = TickFeatureConfig(
        emit_interval_seconds=0.1,
        min_ticks_per_window=1,
        point_size=0.0001,
    )
    bus = TickFeatureBus(maxlen=1)
    health_store = TickFeatureHealthStore(max_snapshot_age_seconds=3)
    engine = TickFeatureEngine(TickFeatureCalculator(config), bus, health_store, config)

    engine.on_tick_batch(_event(_tick(1_000, 1.1000)))
    engine.on_tick_batch(_event(_tick(2_000, 1.1001)))

    stats = bus.stats()
    assert stats["queue_depth"] == 1
    assert stats["dropped_snapshots"] == 1
    assert bus.drain(10)[0].window_end_msc == 2_000


def test_feature_bus_does_not_report_listener_dispatch_as_backlog() -> None:
    config = TickFeatureConfig(
        emit_interval_seconds=0.1,
        min_ticks_per_window=1,
        point_size=0.0001,
    )
    bus = TickFeatureBus(maxlen=10)
    received = []
    bus.add_listener(received.append)
    health_store = TickFeatureHealthStore(max_snapshot_age_seconds=3)
    engine = TickFeatureEngine(TickFeatureCalculator(config), bus, health_store, config)

    engine.on_tick_batch(_event(_tick(1_000, 1.1000)))

    assert [snapshot.window_end_msc for snapshot in received] == [1_000]
    stats = bus.stats()
    assert stats["queue_depth"] == 0
    assert stats["dropped_snapshots"] == 0
    assert bus.drain(10) == []


def test_health_store_marks_symbol_stale_when_snapshot_age_exceeds_limit() -> None:
    config = TickFeatureConfig(
        emit_interval_seconds=0.1,
        min_ticks_per_window=1,
        point_size=0.0001,
    )
    bus = TickFeatureBus(maxlen=10)
    health_store = TickFeatureHealthStore(max_snapshot_age_seconds=1)
    engine = TickFeatureEngine(TickFeatureCalculator(config), bus, health_store, config)

    engine.on_tick_batch(_event(_tick(1_000, 1.1000)))

    health = health_store.health_for(
        "EURUSD",
        now=datetime.fromtimestamp(3, tz=timezone.utc),
        bus_stats=bus.stats(),
    )
    assert health.status == "stale"
    assert health.snapshot_age_seconds == 2.0
    assert health.last_reasons == ("snapshot_stale",)
