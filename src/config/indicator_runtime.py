from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from pydantic import BaseModel

from src.config.indicator_config import normalize_indicator_func_path
from src.config.utils import resolve_config_path
from src.indicators.types import IndicatorTask


class IndicatorSettings(BaseModel):
    poll_seconds: float = 5.0
    reload_interval: float = 60.0
    config_path: Optional[str] = None
    backfill_enabled: bool = True
    backfill_batch_size: int = 1000


@lru_cache
def load_indicator_settings() -> IndicatorSettings:
    from src.config.indicator_config import ConfigLoader

    path = resolve_config_path("indicators.json")
    config = ConfigLoader.load(path) if path else ConfigLoader.load("config/indicators.json")
    return IndicatorSettings(
        poll_seconds=float(config.pipeline.poll_interval),
        reload_interval=float(config.reload_interval),
        config_path=path or "config/indicators.json",
        backfill_enabled=True,
        backfill_batch_size=1000,
    )


def load_indicator_tasks(config_path: Optional[str] = None) -> list[IndicatorTask]:
    from src.config.indicator_config import ConfigLoader

    path = resolve_config_path(config_path or "indicators.json")
    if not path or not os.path.exists(path):
        return []
    config = ConfigLoader.load(path)
    return [
        IndicatorTask(
            name=item.name,
            func_path=normalize_indicator_func_path(item.func_path),
            params=dict(item.params),
        )
        for item in config.indicators
    ]
