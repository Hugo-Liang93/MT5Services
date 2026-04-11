"""Config mutation helpers for IndicatorManager registry."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.config.indicator_config import IndicatorConfig

from .registry_runtime import _register_indicators

if TYPE_CHECKING:
    from ..manager import UnifiedIndicatorManager

logger = logging.getLogger(__name__)


def add_indicator(manager: "UnifiedIndicatorManager", config: IndicatorConfig) -> bool:
    try:
        manager.config_manager.add_indicator(config)
        manager.config = manager.config_manager.get_config()
        _register_indicators(manager)
        logger.info("Added indicator: %s", config.name)
        return True
    except Exception:
        logger.exception("Failed to add indicator %s", config.name)
        return False


def update_indicator(
    manager: "UnifiedIndicatorManager", name: str, **kwargs: Any
) -> bool:
    config = manager.config_manager.get_indicator(name)
    if config is None:
        logger.error("Indicator not found: %s", name)
        return False
    try:
        config_fields = set(getattr(type(config), "__annotations__", {}))
        for key, value in kwargs.items():
            if key in config_fields:
                setattr(config, key, value)
        manager.config_manager.add_indicator(config)
        manager.config = manager.config_manager.get_config()
        _register_indicators(manager)
        logger.info("Updated indicator: %s", name)
        return True
    except Exception:
        logger.exception("Failed to update indicator %s", name)
        return False


def remove_indicator(manager: "UnifiedIndicatorManager", name: str) -> bool:
    try:
        if not manager.config_manager.remove_indicator(name):
            return False
        manager.config = manager.config_manager.get_config()
        manager.dependency_manager.remove_indicator(name)
        manager.state.indicator_funcs.pop(name, None)
        with manager.state.results_lock:
            stale_keys = [key for key in manager.state.results if key.endswith(f"_{name}")]
            for key in stale_keys:
                del manager.state.results[key]
        logger.info("Removed indicator: %s", name)
        return True
    except Exception:
        logger.exception("Failed to remove indicator %s", name)
        return False
