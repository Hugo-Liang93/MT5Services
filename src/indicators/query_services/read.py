"""Read/query and observability helpers for UnifiedIndicatorManager."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from src.utils.common import same_listener_reference

from .runtime import (
    get_intrabar_eligible_names,
    normalize_indicator_group,
    normalize_persisted_indicator_snapshot,
    reconcile_all,
)
from ..runtime.intrabar_metrics import get_intrabar_metrics_snapshot
from .state_view import state_get as _state_get, state_set as _state_set

if TYPE_CHECKING:
    from ..manager import UnifiedIndicatorManager


def _read_manager_config(manager: "UnifiedIndicatorManager") -> Any:
    getter = getattr(manager, "get_config", None)
    if callable(getter):
        return getter()
    return _state_get(manager, "config")


__all__ = [
    "get_indicator",
    "get_all_indicators",
    "get_intrabar_snapshot",
    "get_indicator_info",
    "list_indicators",
    "add_snapshot_listener",
    "remove_snapshot_listener",
    "set_priority_indicator_names",
    "set_priority_indicator_groups",
    "get_performance_stats",
    "clear_cache",
    "get_snapshot",
    "trigger_consistency_check",
    "reset_failed_events",
    "cleanup_old_events",
    "stats",
    "get_dependency_graph",
]


def get_indicator(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    indicator_name: str,
) -> dict[str, Any] | None:
    result_key = f"{symbol}_{timeframe}_{indicator_name}"
    with _state_get(manager, "results_lock"):
        result = _state_get(manager, "results", {}).get(result_key)
    if result:
        enriched = dict(result.value)
        if result.bar_time is not None:
            enriched["_bar_time"] = result.bar_time.isoformat()
        return enriched
    normalized = normalize_persisted_indicator_snapshot(
        manager,
        manager.market_service.latest_indicators(symbol, timeframe),
    )
    return normalized.get(indicator_name)


def get_all_indicators(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
) -> dict[str, dict[str, Any]]:
    prefix = f"{symbol}_{timeframe}_"
    results: dict[str, dict[str, Any]] = {}
    with _state_get(manager, "results_lock"):
        for key, result in _state_get(manager, "results", {}).items():
            if key.startswith(prefix):
                results[key[len(prefix) :]] = result.value
    if results:
        return results
    return normalize_persisted_indicator_snapshot(
        manager,
        manager.market_service.latest_indicators(symbol, timeframe),
    )


def get_intrabar_snapshot(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
) -> tuple[datetime, dict[str, dict[str, float]]] | None:
    cache_key = f"{symbol}_{timeframe}"
    with _state_get(manager, "results_lock"):
        return _state_get(manager, "last_preview_snapshot", {}).get(cache_key)


def get_indicator_info(
    manager: "UnifiedIndicatorManager",
    name: str,
) -> dict[str, Any] | None:
    config = manager.config_manager.get_indicator(name)
    if config is None:
        return None
    return {
        "name": config.name,
        "func_path": config.func_path,
        "params": config.params,
        "dependencies": list(manager.dependency_manager.get_dependencies(name)),
        "dependents": list(manager.dependency_manager.get_dependents(name)),
        "compute_mode": config.compute_mode.value,
        "display": config.display,
        "description": config.description,
        "tags": config.tags,
    }


def list_indicators(manager: "UnifiedIndicatorManager") -> list[dict[str, Any]]:
    config = _read_manager_config(manager)
    configs = []
    if config is not None:
        configs = getattr(config, "indicators", [])
    return [
        info
        for cfg in configs
        if (info := get_indicator_info(manager, cfg.name)) is not None
    ]


def add_snapshot_listener(
    manager: "UnifiedIndicatorManager",
    listener,
) -> None:
    with _state_get(manager, "snapshot_listeners_lock"):
        listeners = list(_state_get(manager, "snapshot_listeners", []))
        listeners.append(listener)
        _state_set(manager, "snapshot_listeners", listeners)


def remove_snapshot_listener(
    manager: "UnifiedIndicatorManager",
    listener,
) -> None:
    with _state_get(manager, "snapshot_listeners_lock"):
        listeners = [
            item
            for item in _state_get(manager, "snapshot_listeners", [])
            if not same_listener_reference(item, listener)
        ]
        _state_set(manager, "snapshot_listeners", listeners)


def set_priority_indicator_names(
    manager: "UnifiedIndicatorManager",
    indicator_names: list[str],
) -> None:
    group = normalize_indicator_group(indicator_names)
    _state_set(manager, "priority_indicator_groups", (group,) if group else ())


def set_priority_indicator_groups(
    manager: "UnifiedIndicatorManager",
    indicator_groups: list[list[str] | tuple[str, ...]],
) -> None:
    ordered: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for indicator_group in indicator_groups:
        group = normalize_indicator_group(list(indicator_group))
        if not group or group in seen:
            continue
        seen.add(group)
        ordered.append(group)
    _state_set(manager, "priority_indicator_groups", tuple(ordered))


def get_performance_stats(manager: "UnifiedIndicatorManager") -> dict[str, Any]:
    pipeline_stats = manager.pipeline.get_stats()
    cache_stats = pipeline_stats.get("cache", {})
    results = _state_get(manager, "results", {})
    config = _read_manager_config(manager)
    config_indicators = list(getattr(config, "indicators", [])) if config else []
    config_symbols = list(getattr(config, "symbols", [])) if config else []
    config_timeframes = list(getattr(config, "timeframes", [])) if config else []
    config_hot_reload = bool(getattr(config, "hot_reload", False)) if config else False
    config_auto_start = bool(getattr(config, "auto_start", False)) if config else False
    pipeline_cfg = getattr(config, "pipeline", None) if config else None
    config_reconcile_interval = (
        getattr(pipeline_cfg, "poll_interval", 0.0)
        if pipeline_cfg is not None
        else 0.0
    )
    with _state_get(manager, "results_lock"):
        result_stats = {
            "total_results": len(results),
            "results_max": _state_get(manager, "results_max"),
            "symbols": len({result.symbol for result in results.values()}),
            "timeframes": len({result.timeframe for result in results.values()}),
            "indicators": len({result.name for result in results.values()}),
        }
    config_stats = {
        "total_indicators": len(config_indicators),
        "display_indicators": len([c for c in config_indicators if c.display]),
        "symbols": len(config_symbols),
        "timeframes": len(config_timeframes),
        "hot_reload": config_hot_reload,
        "auto_start": config_auto_start,
        "reconcile_interval_seconds": config_reconcile_interval,
    }
    return {
        "mode": "event_driven",
        "event_loop_running": bool(
            (_state_get(manager, "event_thread") is not None)
            and _state_get(manager, "event_thread").is_alive()
        ),
        "last_reconcile_at": (
            _state_get(manager, "last_reconcile_at").isoformat()
            if _state_get(manager, "last_reconcile_at")
            else None
        ),
        "total_computations": pipeline_stats.get("total_computations", 0),
        "failed_computations": pipeline_stats.get("failed_computations", 0),
        "cached_computations": pipeline_stats.get("cached_computations", 0),
        "incremental_computations": pipeline_stats.get("incremental_computations", 0),
        "parallel_computations": pipeline_stats.get("parallel_computations", 0),
        "cache_hits": cache_stats.get("hits", 0),
        "cache_misses": cache_stats.get("misses", 0),
        "success_rate": pipeline_stats.get("success_rate", 0),
        "scope_stats": {
            k: dict(v) for k, v in _state_get(manager, "scope_stats", {}).items()
        },
        "confirmed_indicators": sorted(
            _state_get(manager, "confirmed_eligible_cache")
            or [cfg.name for cfg in config_indicators]
        ),
        "intrabar_indicators": sorted(get_intrabar_eligible_names(manager)),
        "event_store": manager.event_store.get_stats(),
        "pipeline": pipeline_stats,
        "results": result_stats,
        "config": config_stats,
        "intrabar": get_intrabar_metrics_snapshot(manager),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def clear_cache(manager: "UnifiedIndicatorManager") -> int:
    cleared = manager.pipeline.clear_cache()
    with _state_get(manager, "results_lock"):
        _state_get(manager, "results", {}).clear()
    return cleared


def get_dependency_graph(manager: "UnifiedIndicatorManager", format: str = "mermaid") -> str:
    return manager.dependency_manager.visualize(format)


def get_snapshot(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
) -> "Any":
    prefix = f"{symbol}_{timeframe}_"
    with _state_get(manager, "results_lock"):
        results = _state_get(manager, "results", {})
        matches = [
            result
            for key, result in results.items()
            if key.startswith(prefix)
        ]
    if not matches:
        from ..manager import IndicatorSnapshot

        persisted = normalize_persisted_indicator_snapshot(
            manager,
            manager.market_service.latest_indicators(symbol, timeframe),
        )
        if not persisted:
            return None
        latest_bar = manager.market_service.get_ohlc_closed(symbol, timeframe, limit=1)
        latest_time = latest_bar[-1].time if latest_bar else None
        return IndicatorSnapshot(
            symbol=symbol,
            timeframe=timeframe,
            data=persisted,
            timestamp=datetime.now(timezone.utc),
            bar_time=latest_time,
            compute_time_ms=0.0,
        )
    latest = max(matches, key=lambda result: result.timestamp)
    return IndicatorSnapshot(
        symbol=latest.symbol,
        timeframe=latest.timeframe,
        data={result.name: result.value for result in matches},
        timestamp=latest.timestamp,
        bar_time=latest.bar_time,
        compute_time_ms=latest.compute_time_ms,
    )


def trigger_consistency_check(manager: "UnifiedIndicatorManager") -> None:
    reconcile_all(manager)


def reset_failed_events(manager: "UnifiedIndicatorManager") -> int:
    return manager.event_store.reset_failed_events()


def cleanup_old_events(manager: "UnifiedIndicatorManager", days_to_keep: int = 7) -> None:
    manager.event_store.cleanup_old_events(days_to_keep)


def stats(manager: "UnifiedIndicatorManager") -> dict[str, Any]:
    return get_performance_stats(manager)
