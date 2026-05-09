from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.app_runtime.builder_phases.signal import _wire_tick_feature_runtime
from src.app_runtime.container import AppContainer
from src.clients.mt5_market import Tick
from src.market.event_bus import TickBatchEvent
from src.utils.common import same_listener_reference


class _MarketService:
    def __init__(self) -> None:
        self.listeners = []

    def add_tick_batch_listener(self, listener) -> None:
        self.listeners.append(listener)

    def remove_tick_batch_listener(self, listener) -> None:
        self.listeners = [
            item for item in self.listeners if not same_listener_reference(item, listener)
        ]

    def add_quote_listener(self, listener) -> None:
        self.listeners.append(listener)

    def remove_quote_listener(self, listener) -> None:
        self.listeners = [
            item for item in self.listeners if not same_listener_reference(item, listener)
        ]


class _SignalRuntime:
    def __init__(self, scopes: tuple[str, ...]) -> None:
        self.scopes = scopes
        self.snapshots = []

    def strategy_capability_contract(self):
        return (
            {
                "name": "tick_probe",
                "valid_scopes": list(self.scopes),
                "needed_indicators": [],
                "needs_intrabar": False,
                "needs_htf": False,
                "regime_affinity": {},
                "htf_requirements": {},
                "market_data_requirements": ["tick"],
            },
        )

    def enqueue_tick_feature_snapshot(self, snapshot):
        self.snapshots.append(snapshot)


def _container(scopes: tuple[str, ...]) -> AppContainer:
    container = AppContainer()
    container.market_service = _MarketService()
    container.signal_runtime = _SignalRuntime(scopes)
    return container


def _tick(time_msc: int) -> Tick:
    return Tick(
        symbol="EURUSD",
        bid=1.1000,
        ask=1.1002,
        last=1.1001,
        volume=1.0,
        time=datetime.fromtimestamp(time_msc / 1000, tz=timezone.utc),
        time_msc=time_msc,
        flags=6,
    )


def test_wires_tick_feature_engine_when_tick_derived_strategy_enabled() -> None:
    container = _container(("tick_derived",))

    _wire_tick_feature_runtime(container)

    assert container.tick_feature_engine is not None
    assert container.tick_feature_bus is not None
    assert container.tick_feature_health_store is not None
    assert len(container.market_service.listeners) == 2


def test_does_not_wire_tick_feature_engine_without_tick_derived_strategy() -> None:
    container = _container(("confirmed",))

    _wire_tick_feature_runtime(container)

    assert container.tick_feature_engine is None
    assert container.tick_feature_bus is None
    assert container.tick_feature_health_store is None
    assert container.market_service.listeners == []


def test_tick_feature_bus_forwards_snapshots_to_signal_runtime() -> None:
    container = _container(("tick_derived",))
    _wire_tick_feature_runtime(container)

    container.tick_feature_engine.on_tick_batch(
        TickBatchEvent(
            symbol="EURUSD",
            ticks=(_tick(1_000), _tick(2_000), _tick(3_000)),
            received_at=datetime.fromtimestamp(3, tz=timezone.utc),
            latest_time_msc=3_000,
        )
    )

    assert len(container.signal_runtime.snapshots) == 1
    assert container.signal_runtime.snapshots[0].symbol == "EURUSD"


def test_tick_feature_cleanup_removes_market_listener_and_bus_listener() -> None:
    container = _container(("tick_derived",))
    _wire_tick_feature_runtime(container)

    for cleanup in reversed(container.shutdown_callbacks):
        cleanup()

    assert container.market_service.listeners == []
    assert container.tick_feature_bus.stats()["listeners"] == 0
