"""Market/infrastructure phase builders."""

from __future__ import annotations

from typing import Any

from src.app_runtime.container import AppContainer
from src.app_runtime.factories import (
    create_indicator_manager,
    create_ingestor,
    create_market_service,
    create_storage_writer,
)
from src.config import load_db_settings, load_mt5_settings, load_storage_settings
from src.monitoring.pipeline import PipelineEventBus, PipelineTraceRecorder


def build_market_layer(
    container: AppContainer,
    *,
    ingest_settings: Any,
    market_settings: Any,
    include_ingestion: bool = True,
    include_indicators: bool = True,
) -> None:
    """Build market/runtime infrastructure for the current instance role."""
    runtime_identity = container.runtime_identity
    mt5_settings = load_mt5_settings()
    db_settings = load_db_settings(
        runtime_identity.environment if runtime_identity is not None else None
    )
    container.market_service = create_market_service(
        mt5_settings,
        market_settings,
    )
    container.storage_writer = create_storage_writer(
        db_settings,
        load_storage_settings(),
    )
    container.market_service.attach_storage(container.storage_writer)
    if include_ingestion:
        container.ingestor = create_ingestor(
            container.market_service,
            container.storage_writer,
            ingest_settings,
        )
    else:
        container.ingestor = None
    if include_indicators:
        container.indicator_manager = create_indicator_manager(
            container.market_service,
            container.storage_writer,
        )
    else:
        container.indicator_manager = None

    container.pipeline_event_bus = PipelineEventBus()
    if container.indicator_manager is not None:
        container.indicator_manager.set_pipeline_event_bus(container.pipeline_event_bus)
    if container.storage_writer is not None:
        container.pipeline_trace_recorder = PipelineTraceRecorder(
            pipeline_bus=container.pipeline_event_bus,
            db_writer=container.storage_writer.db,
            runtime_identity=container.runtime_identity,
        )
