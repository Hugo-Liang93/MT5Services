"""Structured metrics for intrabar queue behaviour."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..manager import UnifiedIndicatorManager


def _to_p95(values: list[float], quantile: float = 0.95) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[int(index)]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def record_intrabar_drop(manager: "UnifiedIndicatorManager") -> None:
    with manager.state.intrabar_metrics_lock:
        manager.state.intrabar_dropped_count += 1


def record_intrabar_queue_age_ms(manager: "UnifiedIndicatorManager", value_ms: float) -> None:
    with manager.state.intrabar_metrics_lock:
        manager.state.intrabar_queue_age_samples_ms.append(float(value_ms))


def record_intrabar_processing_latency_ms(
    manager: "UnifiedIndicatorManager",
    value_ms: float,
) -> None:
    with manager.state.intrabar_metrics_lock:
        manager.state.intrabar_process_latency_samples_ms.append(float(value_ms))


def get_intrabar_metrics_snapshot(manager: "UnifiedIndicatorManager") -> dict[str, int | float]:
    with manager.state.intrabar_metrics_lock:
        dropped = manager.state.intrabar_dropped_count
        ages = list(manager.state.intrabar_queue_age_samples_ms)
        latencies = list(manager.state.intrabar_process_latency_samples_ms)

    return {
        "queue": {
            "current_size": manager.state.intrabar_queue.qsize(),
            "maxsize": manager.state.intrabar_queue.maxsize,
            "dropped_total": dropped,
        },
        "queue_age_ms_p95": _to_p95(ages),
        "processing_latency_ms_p95": _to_p95(latencies),
        "queue_age_sample_count": len(ages),
        "processing_latency_sample_count": len(latencies),
        "queue_age_ms_latest": ages[-1] if ages else 0.0,
        "processing_latency_ms_latest": latencies[-1] if latencies else 0.0,
    }
