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
        # 行情 / 指标 / 经济日历类（行情链路阻断）
        "data_latency",
        "indicator_freshness",
        "queue_depth",
        "economic_calendar_staleness",
        "economic_provider_failures",
        # §0v P2：交易域阻断指标 —— 旧实现漏掉，导致 critical 时只算
        # advisory_critical → overall_status 被降成 warning 掩盖严重故障。
        "reconciliation_lag",
        "circuit_breaker_open",
        "execution_failure_rate",
        "execution_queue_overflows",
    }:
        return "blocking"
    return "advisory"
