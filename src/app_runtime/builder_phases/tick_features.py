from __future__ import annotations

from typing import Any, Callable

from src.app_runtime.container import AppContainer
from src.config import get_tick_feature_config
from src.market.tick_features import (
    TickFeatureBus,
    TickFeatureCalculator,
    TickFeatureConfig,
    TickFeatureEngine,
    TickFeatureHealthStore,
)
from src.market.tick_features.models import TickFeatureSnapshot

TickFeatureListener = Callable[[TickFeatureSnapshot], None]


def ensure_tick_feature_runtime(container: AppContainer) -> bool:
    """Create the shared tick feature runtime once.

    Tick feature production is owned by the market-data runtime, not by signal
    strategies or recovery services. Consumers subscribe through the bus.
    """

    if container.market_service is None:
        return False
    if container.tick_feature_engine is not None:
        return True

    runtime_config = get_tick_feature_config()
    config = TickFeatureConfig(**runtime_config.model_dump())
    feature_bus = TickFeatureBus(maxlen=config.bus_maxlen)
    health_store = TickFeatureHealthStore(
        max_snapshot_age_seconds=config.max_snapshot_age_seconds
    )
    engine = TickFeatureEngine(
        TickFeatureCalculator(config),
        feature_bus,
        health_store,
        config,
    )
    container.market_service.add_tick_batch_listener(engine.on_tick_batch)
    container.market_service.add_quote_listener(engine.on_quote)
    container.tick_feature_engine = engine
    container.tick_feature_bus = feature_bus
    container.tick_feature_health_store = health_store

    def _cleanup() -> None:
        if container.market_service is not None:
            container.market_service.remove_tick_batch_listener(engine.on_tick_batch)
            container.market_service.remove_quote_listener(engine.on_quote)

    container.shutdown_callbacks.append(_cleanup)
    return True


def attach_tick_feature_listener(
    container: AppContainer,
    listener: TickFeatureListener,
) -> bool:
    if not ensure_tick_feature_runtime(container):
        return False
    feature_bus = container.tick_feature_bus
    if feature_bus is None:
        return False
    feature_bus.add_listener(listener)

    def _cleanup() -> None:
        if container.tick_feature_bus is not None:
            container.tick_feature_bus.remove_listener(listener)

    container.shutdown_callbacks.append(_cleanup)
    return True
