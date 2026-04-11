from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .state_view import state_get as _state_get

__all__ = [
    "group_indicator_values",
    "normalize_persisted_indicator_snapshot",
    "store_results",
]


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
    """将持久化的指标快照规范化为 {indicator_name: {metric: value}} 格式。"""
    normalized: Dict[str, Dict[str, Any]] = {}
    for indicator_name, payload in persisted.items():
        if not isinstance(payload, dict):
            continue
        numeric_payload = {
            metric_name: float(raw_value)
            for metric_name, raw_value in payload.items()
            if isinstance(raw_value, (int, float))
        }
        if numeric_payload:
            normalized[indicator_name] = numeric_payload
    return normalized


def store_results(
    manager,
    symbol: str,
    timeframe: str,
    bar_time: Optional[datetime],
    results: Dict[str, Dict[str, Any]],
    compute_time_ms: float,
) -> None:
    from ..manager import IndicatorResult

    timestamp = datetime.now(timezone.utc)
    results_cache = _state_get(manager, "results", None)
    results_max = _state_get(manager, "results_max", None)
    if results_cache is None or results_max is None:
        return

    lock = _state_get(manager, "results_lock", None)
    lock_ctx = lock if lock is not None else nullcontext()
    with lock_ctx:
        for name, value in results.items():
            if value is None:
                continue
            result_key = f"{symbol}_{timeframe}_{name}"
            # Move to end for LRU ordering (delete first if exists)
            if result_key in results_cache:
                results_cache.move_to_end(result_key)
            results_cache[result_key] = IndicatorResult(
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
        while len(results_cache) > int(results_max):
            results_cache.popitem(last=False)
