from __future__ import annotations

from src.indicators.manager import UnifiedIndicatorManager, get_global_unified_manager


def create_indicator_manager(
    market_service,
    storage_writer,
    *,
    config_file: str = "config/indicators.json",
) -> UnifiedIndicatorManager:
    return get_global_unified_manager(
        market_service=market_service,
        storage_writer=storage_writer,
        config_file=config_file,
        start_immediately=False,
    )
