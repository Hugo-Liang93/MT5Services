from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from src.config import get_interval_config, get_limit_config, get_shared_default_symbol


def runtime_market_defaults() -> dict:
    limits = get_limit_config()
    intervals = get_interval_config()
    return {
        "default_symbol": get_shared_default_symbol(),
        "tick_limit": limits.tick_limit,
        "ohlc_limit": limits.ohlc_limit,
        "stream_interval_seconds": intervals.stream_interval,
    }


def normalize_query_time(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def model_payload(item) -> dict:
    payload = dict(getattr(item, "__dict__", {}) or {})
    if getattr(item, "time", None) is not None:
        payload["time"] = item.time.isoformat()
    return payload
