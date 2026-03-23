from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


def group_indicator_values(
    manager,
    results: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, float]]:
    grouped: Dict[str, Dict[str, float]] = {}
    for indicator_name, payload in results.items():
        if not isinstance(payload, dict) or not payload:
            continue
        normalized_payload: Dict[str, float] = {}
        for metric_name, raw_value in payload.items():
            if not isinstance(raw_value, (int, float)):
                continue
            normalized_payload[metric_name] = float(raw_value)
        if normalized_payload:
            grouped[indicator_name] = normalized_payload
    return grouped


def normalize_persisted_indicator_snapshot(
    manager,
    persisted: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    normalized: Dict[str, Dict[str, Any]] = {}
    consumed_keys: set[str] = set()

    for indicator_name, payload in persisted.items():
        if not isinstance(payload, dict):
            continue
        numeric_payload = {
            metric_name: float(raw_value)
            for metric_name, raw_value in payload.items()
            if isinstance(raw_value, (int, float))
        }
        if not numeric_payload:
            continue
        normalized[indicator_name] = numeric_payload
        consumed_keys.add(indicator_name)

    legacy_scalars = {
        key: float(value)
        for key, value in persisted.items()
        if isinstance(value, (int, float))
    }
    configured_names = [config.name for config in manager.config.indicators if config.enabled]

    for indicator_name in configured_names:
        direct_value = legacy_scalars.get(indicator_name)
        prefixed_values = {
            metric_name[len(indicator_name) + 1 :]: value
            for metric_name, value in legacy_scalars.items()
            if metric_name.startswith(f"{indicator_name}_")
        }
        if direct_value is None and not prefixed_values:
            continue

        payload = dict(normalized.get(indicator_name, {}))
        if direct_value is not None:
            payload[indicator_name] = direct_value
            consumed_keys.add(indicator_name)
        payload.update(prefixed_values)
        consumed_keys.update(
            metric_name
            for metric_name in legacy_scalars
            if metric_name == indicator_name
            or metric_name.startswith(f"{indicator_name}_")
        )
        normalized[indicator_name] = payload

    for key, value in legacy_scalars.items():
        if key in consumed_keys:
            continue
        normalized[key] = {key: value}

    return normalized


def store_results(
    manager,
    symbol: str,
    timeframe: str,
    bar_time: Optional[datetime],
    results: Dict[str, Dict[str, Any]],
    compute_time_ms: float,
) -> None:
    from .manager import IndicatorResult

    timestamp = datetime.now(timezone.utc)
    with manager._results_lock:
        for name, value in results.items():
            if value is None:
                continue
            result_key = f"{symbol}_{timeframe}_{name}"
            # Move to end for LRU ordering (delete first if exists)
            if result_key in manager._results:
                manager._results.move_to_end(result_key)
            manager._results[result_key] = IndicatorResult(
                name=name,
                value=value,
                symbol=symbol,
                timeframe=timeframe,
                timestamp=timestamp,
                bar_time=bar_time,
                cache_hit=False,
                incremental=False,
                compute_time_ms=compute_time_ms,
                success=True,
            )
        # LRU eviction
        while len(manager._results) > manager._results_max:
            manager._results.popitem(last=False)
