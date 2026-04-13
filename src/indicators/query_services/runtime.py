"""Runtime compute and event helpers for UnifiedIndicatorManager."""

from __future__ import annotations

import logging
from datetime import datetime
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ..runtime.bar_loader import (
    get_max_lookback as _get_max_lookback_fn,
    get_min_required_history as _get_min_required_history_fn,
    indicator_history_requirement as _indicator_history_requirement_fn,
    reconcile_min_bars as _reconcile_min_bars_fn,
    resolve_indicator_names as _resolve_indicator_names_fn,
)
from ..runtime.delta_metrics import apply_delta_metrics as _apply_delta_metrics_fn
from ..runtime.delta_metrics import get_delta_config as _get_delta_config_fn
from ..runtime.delta_metrics import (
    merge_snapshot_metrics_into_results as _merge_snapshot_metrics_fn,
)
from ..runtime.pipeline_runner import (
    compute_priority_results as _compute_priority_results_fn,
    compute_results_with_priority_groups as _compute_results_with_priority_groups_fn,
    compute_with_bars as _compute_with_bars_fn,
    run_pipeline as _run_pipeline_fn,
)
from .storage import (
    group_indicator_values as _group_indicator_values_fn,
    normalize_persisted_indicator_snapshot as _normalize_persisted_indicator_snapshot_fn,
    store_results as _store_results_fn,
)
from .state_view import state_get as _state_get, state_set as _state_set

if TYPE_CHECKING:
    from ..manager import UnifiedIndicatorManager

logger = logging.getLogger(__name__)

_process_closed_bar_event_fn = None
_process_intrabar_event_fn = None
_process_closed_bar_events_batch_fn = None
_UNSET = object()


def _ensure_bar_event_handlers() -> None:
    """Lazily bind runtime event handlers to avoid import cycles."""
    global _process_closed_bar_event_fn
    global _process_intrabar_event_fn
    global _process_closed_bar_events_batch_fn

    if (
        _process_closed_bar_event_fn is not None
        and _process_intrabar_event_fn is not None
        and _process_closed_bar_events_batch_fn is not None
    ):
        return
    from ..runtime.bar_event_handler import (
        process_closed_bar_event as _cb_handler,
        process_intrabar_event as _ib_handler,
        process_closed_bar_events_batch as _batch_handler,
    )

    _process_closed_bar_event_fn = _cb_handler
    _process_intrabar_event_fn = _ib_handler
    _process_closed_bar_events_batch_fn = _batch_handler

__all__ = [
    "resolve_indicator_names",
    "indicator_history_requirement",
    "indicator_requirements",
    "select_indicator_names_for_history",
    "mark_event_skipped",
    "mark_event_completed",
    "mark_event_failed",
    "run_pipeline_query",
    "compute_with_bars_query",
    "load_confirmed_bars",
    "normalize_indicator_group",
    "load_intrabar_bars",
    "group_indicator_values",
    "indicator_delta_config",
    "apply_delta_metrics_query",
    "merge_snapshot_metrics_into_results",
    "normalize_persisted_indicator_snapshot",
    "store_results",
    "store_preview_snapshot",
    "write_back_results",
    "publish_snapshot",
    "publish_intrabar_snapshot",
    "compute_results_with_priority_groups",
    "compute_priority_results",
    "compute_confirmed_results_for_bars",
    "compute_intrabar_results_for_bars",
    "process_closed_bar_event",
    "process_closed_bar_events_batch",
    "set_confirmed_eligible_override",
    "get_confirmed_eligible_names",
    "set_intrabar_eligible_override",
    "get_intrabar_eligible_names",
    "process_intrabar_event",
    "get_max_lookback",
    "get_min_required_history",
    "reconcile_min_bars",
    "is_reconcile_target_ready",
    "has_reconcile_ready_targets",
    "reconcile_all",
    "reconcile_symbol_timeframe",
    "compute",
    "_resolve_indicator_names",
    "_indicator_history_requirement",
    "_indicator_requirements",
    "_select_indicator_names_for_history",
    "_mark_event_skipped",
    "_mark_event_completed",
    "_mark_event_failed",
    "_run_pipeline",
    "_compute_with_bars",
    "_load_confirmed_bars",
    "_normalize_indicator_group",
    "_load_intrabar_bars",
    "_group_indicator_values",
    "_indicator_delta_config",
    "_apply_delta_metrics",
    "_merge_snapshot_metrics_into_results",
    "_normalize_persisted_indicator_snapshot",
    "_store_results",
    "_store_preview_snapshot",
    "_write_back_results",
    "_publish_snapshot",
    "_publish_intrabar_snapshot",
    "_compute_results_with_priority_groups",
    "_compute_priority_results",
    "_compute_confirmed_results_for_bars",
    "_compute_intrabar_results_for_bars",
    "_process_closed_bar_event",
    "_process_closed_bar_events_batch",
    "_get_confirmed_eligible_names",
    "_get_intrabar_eligible_names",
    "_process_intrabar_event",
    "_reconcile_all",
    "_reconcile_symbol_timeframe",
    "_get_max_lookback",
    "_get_min_required_history",
    "_reconcile_min_bars",
    "_is_reconcile_target_ready",
    "_has_reconcile_ready_targets",
]


def _state_or_attr(manager, name: str, default=_UNSET):
    try:
        value = _state_get(manager, name, _UNSET)
    except AttributeError:
        value = _UNSET
    if value is not _UNSET:
        return value
    if name == "config":
        raw_config = vars(manager).get("config", _UNSET)
        if raw_config is not _UNSET:
            return raw_config
        resolver = getattr(manager, "get_config", None)
        if callable(resolver):
            try:
                return resolver()
            except Exception:
                return default
    return default


def _get_manager_config(manager):
    config = _state_or_attr(manager, "config", _UNSET)
    if config is _UNSET:
        return None
    return config


def _get_pipeline_bus(manager):
    try:
        runtime_bus = _state_get(manager, "pipeline_event_bus", _UNSET)
    except AttributeError:
        runtime_bus = _UNSET
    if runtime_bus is not _UNSET:
        return runtime_bus
    resolver = getattr(manager, "get_pipeline_event_bus", None)
    if callable(resolver):
        try:
            return resolver()
        except Exception:
            logger.debug(
                "Failed to resolve pipeline_event_bus via get_pipeline_event_bus",
                exc_info=True,
            )
    return None


def _get_indicator_configs(manager) -> list[Any]:
    configs = _state_get(manager, "indicator_configs", _UNSET)
    if configs is _UNSET:
        config = _get_manager_config(manager)
        if config is not None:
            configs = getattr(config, "indicators", _UNSET)
        else:
            configs = _state_get(manager, "indicators", _UNSET)
    if configs is _UNSET:
        configs = vars(manager).get("_indicator_configs", _UNSET)
    if configs is _UNSET:
        return []
    return list(configs or [])


def _manager_logger(manager: "UnifiedIndicatorManager") -> logging.Logger:
    return logger


def resolve_indicator_names(
    manager: "UnifiedIndicatorManager",
    indicator_names: list[str] | None = None,
) -> list[str]:
    override = vars(manager).get("_resolve_indicator_names", _UNSET)
    if override is not _UNSET and callable(override):
        return list(override(indicator_names))
    configs = _get_indicator_configs(manager)
    return _resolve_indicator_names_fn(configs, indicator_names)


def indicator_history_requirement(
    manager: "UnifiedIndicatorManager",
    config,
) -> int:
    if config is None:
        return 2
    return _indicator_history_requirement_fn(config)


def indicator_requirements(
    manager: "UnifiedIndicatorManager",
    indicator_names: list[str] | None = None,
) -> dict[str, int]:
    selected_names = set(resolve_indicator_names(manager, indicator_names))
    configs = _get_indicator_configs(manager)
    requirements: dict[str, int] = {}
    for cfg in configs:
        if cfg.name not in selected_names:
            continue
        requirements[cfg.name] = _indicator_history_requirement_fn(cfg)
    return requirements


def select_indicator_names_for_history(
    manager: "UnifiedIndicatorManager",
    available_bars: int,
    indicator_names: list[str] | None = None,
) -> list[str]:
    reqs = indicator_requirements(manager, indicator_names)
    return [
        name
        for name in resolve_indicator_names(manager, indicator_names)
        if reqs.get(name, 2) <= available_bars
    ]


def mark_event_skipped(
    manager: "UnifiedIndicatorManager", event_id: int, reason: str
) -> None:
    manager.event_store.mark_event_skipped_by_id(event_id, reason)


def mark_event_completed(manager: "UnifiedIndicatorManager", event_id: int) -> None:
    manager.event_store.mark_event_completed_by_id(event_id)


def mark_event_failed(
    manager: "UnifiedIndicatorManager", event_id: int, error: str
) -> None:
    manager.event_store.mark_event_failed_by_id(event_id, error)


def run_pipeline_query(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    indicator_names: list[str] | None = None,
    bar_time: datetime | None = None,
) -> tuple[list[Any], dict[str, dict[str, Any]], float]:
    return _run_pipeline_fn(
        manager,
        symbol,
        timeframe,
        indicator_names=indicator_names,
        bar_time=bar_time,
    )


def compute_with_bars_query(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    bars: list[Any],
    indicator_names: list[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], float, list[str]]:
    return _compute_with_bars_fn(
        manager,
        symbol,
        timeframe,
        bars,
        indicator_names=indicator_names,
    )


def normalize_indicator_group(indicator_names: list[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for indicator_name in indicator_names:
        if indicator_name in seen:
            continue
        seen.add(indicator_name)
        ordered.append(indicator_name)
    return tuple(ordered)


def load_confirmed_bars(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    bar_time: datetime | None = None,
) -> list[Any]:
    lookback = get_max_lookback(manager)
    if lookback <= 0:
        return []
    if bar_time is None:
        return manager.market_service.get_ohlc_closed(
            symbol,
            timeframe,
            limit=lookback,
        )

    bars = manager.market_service.get_ohlc_window(
        symbol,
        timeframe,
        end_time=bar_time,
        limit=lookback,
    )
    if not bars or bars[-1].time != bar_time:
        return []
    return bars


def load_intrabar_bars(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    bar: Any,
) -> list[Any]:
    lookback = get_max_lookback(manager)
    if lookback <= 0:
        return []
    closed_limit = max(lookback - 1, 1)
    closed_bars = manager.market_service.get_ohlc_closed(
        symbol, timeframe, limit=closed_limit
    )
    preview_bars = [item for item in closed_bars if item.time != bar.time]
    preview_bars.append(bar)
    return preview_bars[-lookback:]


def group_indicator_values(
    manager: "UnifiedIndicatorManager",
    results: dict[str, dict[str, Any]],
) -> dict[str, dict[str, float]]:
    return _group_indicator_values_fn(manager, results)


def indicator_delta_config(manager: "UnifiedIndicatorManager") -> dict[str, tuple[int, ...]]:
    config_items = _get_indicator_configs(manager)
    return _get_delta_config_fn(config_items)


def apply_delta_metrics_query(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    indicators: dict[str, dict[str, float]],
    *,
    bar_time: datetime | None = None,
) -> dict[str, dict[str, float]]:
    delta_config = indicator_delta_config(manager)

    def _load_history(sym: str, tf: str, bt: datetime | None, count: int) -> list[Any]:
        if bt is not None:
            return list(
                manager.market_service.get_ohlc_window(sym, tf, end_time=bt, limit=count)
            )
        return list(manager.market_service.get_ohlc_closed(sym, tf, limit=count))

    return _apply_delta_metrics_fn(
        symbol,
        timeframe,
        indicators,
        delta_config,
        _load_history,
        bar_time=bar_time,
    )


def merge_snapshot_metrics_into_results(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    indicators: dict[str, dict[str, float]],
) -> None:
    results = _state_get(manager, "results")
    if results is None:
        return
    results_lock = _state_get(manager, "results_lock")
    if results_lock is None:
        return
    _merge_snapshot_metrics_fn(
        symbol,
        timeframe,
        indicators,
        results,
        results_lock,
    )


def normalize_persisted_indicator_snapshot(
    manager: "UnifiedIndicatorManager",
    persisted: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    return _normalize_persisted_indicator_snapshot_fn(manager, persisted)


def store_results(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    bar_time: datetime | None,
    results: dict[str, dict[str, Any]],
    compute_time_ms: float,
) -> None:
    _store_results_fn(
        manager,
        symbol,
        timeframe,
        bar_time,
        results,
        compute_time_ms,
    )


def store_preview_snapshot(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    bar_time: datetime,
    indicators: dict[str, dict[str, float]],
) -> bool:
    cache_key = f"{symbol}_{timeframe}"
    normalized = {name: dict(payload) for name, payload in indicators.items()}
    results_lock = _state_get(manager, "results_lock")
    lock_ctx = results_lock if results_lock is not None else nullcontext()
    snapshot_cache = _state_get(manager, "last_preview_snapshot")
    if snapshot_cache is None:
        return True

    max_entries = _state_get(manager, "preview_snapshot_max_entries", 500)

    with lock_ctx:
        current = snapshot_cache.get(cache_key)
        if (
            current is not None
            and current[0] == bar_time
            and current[1] == normalized
        ):
            return False
        snapshot_cache.pop(cache_key, None)
        snapshot_cache[cache_key] = (bar_time, normalized)
        while len(snapshot_cache) > max_entries:
            snapshot_cache.popitem(last=False)
    return True


def write_back_results(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    bars: list[Any],
    results: dict[str, dict[str, Any]],
    compute_time_ms: float,
    bar_time: datetime | None = None,
) -> dict[str, dict[str, float]]:
    effective_bar_time = bar_time or (bars[-1].time if bars else None)
    store_results(manager, symbol, timeframe, effective_bar_time, results, compute_time_ms)

    if not bars or effective_bar_time is None:
        return {}

    grouped = group_indicator_values(manager, results)
    if not grouped:
        return {}

    grouped = apply_delta_metrics_query(
        manager,
        symbol,
        timeframe,
        grouped,
        bar_time=effective_bar_time,
    )
    merge_snapshot_metrics_into_results(manager, symbol, timeframe, grouped)

    latest_bar = bars[-1]
    update_cache = getattr(manager.market_service, "update_ohlc_indicators", None)
    if callable(update_cache):
        update_cache(
            symbol,
            timeframe,
            effective_bar_time,
            grouped,
        )

    storage_writer = getattr(manager, "storage_writer", None)
    if storage_writer is not None:
        row = (
            latest_bar.symbol,
            latest_bar.timeframe,
            latest_bar.open,
            latest_bar.high,
            latest_bar.low,
            latest_bar.close,
            latest_bar.volume,
            latest_bar.time.isoformat(),
            dict(latest_bar.indicators or {}),
        )
        storage_writer.enqueue("ohlc_indicators", row)

    override_snapshot = vars(manager).get("_publish_snapshot", _UNSET)
    if override_snapshot is not _UNSET:
        override_snapshot(
            symbol,
            timeframe,
            effective_bar_time,
            grouped,
            scope="confirmed",
        )
    else:
        publish_snapshot(
            manager,
            symbol,
            timeframe,
            effective_bar_time,
            grouped,
            scope="confirmed",
        )
    return grouped


def publish_snapshot(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    bar_time: datetime,
    indicators: dict[str, dict[str, float]],
    *,
    scope: str,
) -> None:
    logger = _manager_logger(manager)
    try:
        trace_id = manager.get_current_trace_id()
    except Exception:
        trace_id = None
    owns_trace_id = not bool(str(trace_id or "").strip())
    if owns_trace_id:
        trace_id = uuid4().hex
        try:
            manager.set_current_trace_id(trace_id)
        except Exception:
            logger.debug(
                "Failed to set fallback snapshot trace_id for %s/%s",
                symbol,
                timeframe,
                exc_info=True,
            )
    pipeline_bus = _get_pipeline_bus(manager)
    if pipeline_bus is not None and trace_id is not None:
        pipeline_bus.emit_snapshot_published(
            trace_id, symbol, timeframe, scope, len(indicators), bar_time,
            indicators=indicators,
        )

    lock = _state_get(manager, "snapshot_listeners_lock")
    if lock is None:
        listeners = list(_state_get(manager, "snapshot_listeners", []))
    else:
        with lock:
            listeners = list(_state_get(manager, "snapshot_listeners", []))
    for listener in listeners:
        try:
            listener(symbol, timeframe, bar_time, indicators, scope)
        except Exception:
            logger.exception(
                "Failed to publish indicator snapshot for %s/%s at %s",
                symbol,
                timeframe,
                bar_time,
            )
    if owns_trace_id:
        try:
            manager.set_current_trace_id(None)
        except Exception:
            logger.debug(
                "Failed to clear fallback snapshot trace_id for %s/%s",
                symbol,
                timeframe,
                exc_info=True,
            )


def _configured_symbols(manager: "UnifiedIndicatorManager") -> list[str]:
    symbols = vars(manager).get("_configured_symbols", _UNSET)
    if symbols is _UNSET:
        cfg = _get_manager_config(manager)
        if cfg is None:
            symbols = _state_or_attr(manager, "symbols", _UNSET)
        else:
            symbols = getattr(cfg, "symbols", _UNSET)
    if symbols is _UNSET or symbols is None:
        return []
    return list(symbols)


def _configured_timeframes(manager: "UnifiedIndicatorManager") -> list[str]:
    timeframes = vars(manager).get("_configured_timeframes", _UNSET)
    if timeframes is _UNSET:
        cfg = _get_manager_config(manager)
        if cfg is None:
            timeframes = _state_or_attr(manager, "timeframes", _UNSET)
        else:
            timeframes = getattr(cfg, "timeframes", _UNSET)
    if timeframes is _UNSET or timeframes is None:
        return []
    return list(timeframes)


def publish_intrabar_snapshot(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    bar_time: datetime,
    indicators: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]]:
    enriched = apply_delta_metrics_query(
        manager,
        symbol,
        timeframe,
        {name: dict(payload) for name, payload in indicators.items()},
        bar_time=bar_time,
    )
    if not indicators:
        return {}

    if not store_preview_snapshot(manager, symbol, timeframe, bar_time, enriched):
        return enriched

    publish_snapshot(
        manager,
        symbol,
        timeframe,
        bar_time,
        enriched,
        scope="intrabar",
    )
    return enriched


def compute_results_with_priority_groups(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    bars: list[Any],
    *,
    bar_time: datetime,
    scope: str,
    indicator_names: list[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], float]:
    return _compute_results_with_priority_groups_fn(
        manager,
        symbol,
        timeframe,
        bars,
        bar_time=bar_time,
        scope=scope,
        indicator_names=indicator_names,
    )


def compute_priority_results(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    bars: list[Any],
    *,
    bar_time: datetime,
    scope: str,
    selected_names: list[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], float, set[str]]:
    return _compute_priority_results_fn(
        manager,
        symbol,
        timeframe,
        bars,
        bar_time=bar_time,
        scope=scope,
        selected_names=selected_names,
    )


def compute_confirmed_results_for_bars(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    bars: list[Any],
    *,
    bar_time: datetime,
) -> tuple[dict[str, dict[str, Any]], float]:
    eligible = get_confirmed_eligible_names(manager)
    results, compute_time = compute_results_with_priority_groups(
        manager,
        symbol,
        timeframe,
        bars,
        bar_time=bar_time,
        scope="confirmed",
        indicator_names=eligible,
    )
    lock = _state_get(manager, "scope_stats_lock")
    scope_stats = _state_get(manager, "scope_stats")
    if scope_stats is None:
        return results, compute_time
    if lock is None:
        return results, compute_time
    with lock:
        scope_stats["confirmed"]["computations"] += 1
        scope_stats["confirmed"]["indicators"] += len(results)
    return results, compute_time


def compute_intrabar_results_for_bars(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    bars: list[Any],
    *,
    bar_time: datetime,
    indicator_names: list[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], float]:
    results, compute_time = compute_results_with_priority_groups(
        manager,
        symbol,
        timeframe,
        bars,
        bar_time=bar_time,
        scope="intrabar",
        indicator_names=indicator_names,
    )
    lock = _state_get(manager, "scope_stats_lock")
    scope_stats = _state_get(manager, "scope_stats")
    if scope_stats is None:
        return results, compute_time
    if lock is None:
        return results, compute_time
    with lock:
        scope_stats["intrabar"]["computations"] += 1
        scope_stats["intrabar"]["indicators"] += len(results)
    return results, compute_time


def process_closed_bar_event(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    bar_time: datetime,
    durable_event: bool,
) -> None:
    _ensure_bar_event_handlers()
    if _process_closed_bar_event_fn is None:
        raise RuntimeError("Closed bar event handler is not initialized")
    _process_closed_bar_event_fn(manager, symbol, timeframe, bar_time, durable_event)


def process_closed_bar_events_batch(
    manager: "UnifiedIndicatorManager",
    events: list,
    durable_event: bool,
) -> None:
    _ensure_bar_event_handlers()
    if _process_closed_bar_events_batch_fn is None:
        raise RuntimeError("Batch closed-bar handler is not initialized")
    _process_closed_bar_events_batch_fn(manager, events, durable_event)


def set_confirmed_eligible_override(
    manager: "UnifiedIndicatorManager",
    names: frozenset,
) -> None:
    registered = frozenset(cfg.name for cfg in _get_indicator_configs(manager))
    _state_set(manager, "confirmed_eligible_cache", names & registered)
    _manager_logger(manager).info(
        "Confirmed eligible indicators (auto-derived from strategies + infra): %s",
        sorted(_state_get(manager, "confirmed_eligible_cache", frozenset())),
    )


def get_confirmed_eligible_names(
    manager: "UnifiedIndicatorManager",
) -> list[str] | None:
    cache = _state_get(manager, "confirmed_eligible_cache", None)
    if not cache:
        return None
    return list(cache)


def set_intrabar_eligible_override(
    manager: "UnifiedIndicatorManager",
    names: frozenset,
) -> None:
    registered = frozenset(cfg.name for cfg in _get_indicator_configs(manager))
    _state_set(manager, "intrabar_eligible_cache", names & registered)
    _manager_logger(manager).info(
        "Intrabar eligible indicators (auto-derived from strategy scopes): %s",
        sorted(_state_get(manager, "intrabar_eligible_cache", frozenset())),
    )


def get_intrabar_eligible_names(manager: "UnifiedIndicatorManager") -> frozenset:
    return _state_get(manager, "intrabar_eligible_cache", frozenset()) or frozenset()


def process_intrabar_event(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    bar: Any,
) -> dict[str, dict[str, float]]:
    _ensure_bar_event_handlers()
    if _process_intrabar_event_fn is None:
        raise RuntimeError("Intrabar event handler is not initialized")
    return _process_intrabar_event_fn(manager, symbol, timeframe, bar)


def get_max_lookback(manager: "UnifiedIndicatorManager") -> int:
    override = vars(manager).get("_get_max_lookback", _UNSET)
    if override is not _UNSET and callable(override):
        return int(override())
    configs = _get_indicator_configs(manager)
    if not configs:
        return 0
    return _get_max_lookback_fn(configs)


def get_min_required_history(manager: "UnifiedIndicatorManager") -> int:
    override = vars(manager).get("_get_min_required_history", _UNSET)
    if override is not _UNSET and callable(override):
        return int(override())
    configs = _get_indicator_configs(manager)
    if not configs:
        return 0
    return _get_min_required_history_fn(configs)


def reconcile_min_bars(manager: "UnifiedIndicatorManager") -> int:
    override = vars(manager).get("_reconcile_min_bars", _UNSET)
    if override is not _UNSET and callable(override):
        return int(override())
    configs = _get_indicator_configs(manager)
    if not configs:
        return 0
    return _reconcile_min_bars_fn(configs)


def is_reconcile_target_ready(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
) -> bool:
    return manager.market_service.has_cached_ohlc(
        symbol,
        timeframe,
        minimum_bars=reconcile_min_bars(manager),
    )


def has_reconcile_ready_targets(manager: "UnifiedIndicatorManager") -> bool:
    for symbol in _configured_symbols(manager):
        for timeframe in _configured_timeframes(manager):
            if is_reconcile_target_ready(manager, symbol, timeframe):
                return True
    return False


def reconcile_all(manager: "UnifiedIndicatorManager") -> None:
    for symbol in _configured_symbols(manager):
        for timeframe in _configured_timeframes(manager):
            if not is_reconcile_target_ready(manager, symbol, timeframe):
                continue
            try:
                reconcile_symbol_timeframe(manager, symbol, timeframe)
            except Exception:
                _manager_logger(manager).exception(
                    "Indicator reconcile failed for %s/%s",
                    symbol,
                    timeframe,
                )


def reconcile_symbol_timeframe(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
) -> None:
    bars = load_confirmed_bars(manager, symbol, timeframe)
    if not bars:
        return
    results, compute_time_ms = compute_results_with_priority_groups(
        manager,
        symbol,
        timeframe,
        bars,
        bar_time=bars[-1].time,
        scope="confirmed",
    )
    lock = _state_get(manager, "scope_stats_lock")
    scope_stats = _state_get(manager, "scope_stats")
    if scope_stats is not None and lock is not None:
        with lock:
            scope_stats["reconcile"]["computations"] += 1
            scope_stats["reconcile"]["indicators"] += len(results)
    write_back_results(manager, symbol, timeframe, bars, results, compute_time_ms)


def compute(
    manager: "UnifiedIndicatorManager",
    symbol: str,
    timeframe: str,
    indicator_names: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    bars, results, compute_time_ms = run_pipeline_query(
        manager,
        symbol,
        timeframe,
        indicator_names=indicator_names,
    )
    if not bars:
        _manager_logger(manager).warning(
            "Insufficient data for computation: %s/%s",
            symbol,
            timeframe,
        )
        return {}
    write_back_results(manager, symbol, timeframe, bars, results, compute_time_ms)
    return results


# ---- Internal runtime method aliases（供 QueryBindingMixin 显式绑定）
_resolve_indicator_names = resolve_indicator_names
_indicator_history_requirement = indicator_history_requirement
_indicator_requirements = indicator_requirements
_select_indicator_names_for_history = select_indicator_names_for_history
_mark_event_skipped = mark_event_skipped
_mark_event_completed = mark_event_completed
_mark_event_failed = mark_event_failed
_run_pipeline = run_pipeline_query
_compute_with_bars = compute_with_bars_query
_load_confirmed_bars = load_confirmed_bars
_normalize_indicator_group = normalize_indicator_group
_load_intrabar_bars = load_intrabar_bars
_group_indicator_values = group_indicator_values
_indicator_delta_config = indicator_delta_config
_apply_delta_metrics = apply_delta_metrics_query
_merge_snapshot_metrics_into_results = merge_snapshot_metrics_into_results
_normalize_persisted_indicator_snapshot = normalize_persisted_indicator_snapshot
_store_results = store_results
_store_preview_snapshot = store_preview_snapshot
_write_back_results = write_back_results
_publish_snapshot = publish_snapshot
_publish_intrabar_snapshot = publish_intrabar_snapshot
_compute_results_with_priority_groups = compute_results_with_priority_groups
_compute_priority_results = compute_priority_results
_compute_confirmed_results_for_bars = compute_confirmed_results_for_bars
_compute_intrabar_results_for_bars = compute_intrabar_results_for_bars
_process_closed_bar_event = process_closed_bar_event
_process_closed_bar_events_batch = process_closed_bar_events_batch
_get_confirmed_eligible_names = get_confirmed_eligible_names
_get_intrabar_eligible_names = get_intrabar_eligible_names
_process_intrabar_event = process_intrabar_event
_reconcile_all = reconcile_all
_reconcile_symbol_timeframe = reconcile_symbol_timeframe
_get_max_lookback = get_max_lookback
_get_min_required_history = get_min_required_history
_reconcile_min_bars = reconcile_min_bars
_is_reconcile_target_ready = is_reconcile_target_ready
_has_reconcile_ready_targets = has_reconcile_ready_targets
