"""Runtime registration helpers for IndicatorManager."""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any

from src.config.indicator_config import ComputeMode
from src.config.indicator_config import IndicatorConfig

from ..cache.incremental import IncrementalIndicator
from ..engine.dependency_manager import get_global_dependency_manager
from ..engine.pipeline import get_global_pipeline
from ..monitoring.metrics_collector import get_global_collector

if TYPE_CHECKING:
    from ..manager import UnifiedIndicatorManager

logger = logging.getLogger(__name__)


def _init_components(manager: "UnifiedIndicatorManager") -> None:
    """Initialize runtime pipeline, dependency manager and metrics collector."""
    manager.pipeline = get_global_pipeline(manager.config.pipeline)
    manager.pipeline.update_config(manager.config.pipeline)
    manager.dependency_manager = get_global_dependency_manager()
    manager.dependency_manager.clear()
    manager.metrics_collector = get_global_collector()


def _load_indicator_func(
    manager: "UnifiedIndicatorManager",
    config: IndicatorConfig,
) -> Any:
    cached = manager.state.indicator_funcs.get(config.name)
    if cached is not None:
        return cached
    module_path, func_name = config.func_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, func_name)


def _load_incremental_class(
    manager: "UnifiedIndicatorManager",
    config: IndicatorConfig,
) -> type | None:
    if config.compute_mode != ComputeMode.INCREMENTAL:
        return None
    module_path, func_name = config.func_path.rsplit(".", 1)
    class_name = func_name.capitalize() + "Incremental"
    try:
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name, None)
        if cls is None or not issubclass(cls, IncrementalIndicator):
            logger.warning(
                "No IncrementalIndicator subclass '%s' in %s for '%s'; "
                "falling back to standard full-recompute",
                class_name,
                module_path,
                config.name,
            )
            return None
        return cls
    except (ImportError, AttributeError, TypeError) as exc:
        logger.warning("Failed to load incremental class for '%s': %s", config.name, exc)
        return None


def _register_indicators(manager: "UnifiedIndicatorManager") -> None:
    manager.state.indicator_funcs.clear()
    manager.state.intrabar_eligible_cache = None
    for indicator_config in manager.config.indicators:
        func = _load_indicator_func(manager, indicator_config)
        incremental_class = _load_incremental_class(manager, indicator_config)
        manager.pipeline.register_indicator(
            name=indicator_config.name,
            func=func,
            params=indicator_config.params,
            dependencies=indicator_config.dependencies or None,
            incremental_class=incremental_class,
        )
        if indicator_config.cache_ttl is not None:
            manager.dependency_manager.indicator_cache_ttl[indicator_config.name] = (
                indicator_config.cache_ttl
            )
        manager.state.indicator_funcs[indicator_config.name] = func

    manager.state.intrabar_eligible_cache = frozenset()

    registered_names = set(manager.state.indicator_funcs)
    for cfg in manager.config.indicators:
        for dep in cfg.dependencies or []:
            if dep not in registered_names:
                logger.warning(
                    "Indicator '%s' declares dependency '%s' which is not registered. "
                    "Computation of '%s' will fail at runtime until '%s' is registered.",
                    cfg.name,
                    dep,
                    cfg.name,
                    dep,
                )


def _reinitialize(manager: "UnifiedIndicatorManager") -> None:
    logger.info("Reinitializing indicator manager after config reload")
    manager.state.last_preview_snapshot.clear()
    manager.clear_cache()
    _init_components(manager)
    _register_indicators(manager)
