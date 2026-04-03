from __future__ import annotations

import math
from typing import Any


def is_finite_metric_value(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def metric_overall_impact(metric_name: str) -> str:
    if metric_name in {
        "data_latency",
        "indicator_freshness",
        "queue_depth",
        "economic_calendar_staleness",
        "economic_provider_failures",
    }:
        return "blocking"
    return "advisory"
