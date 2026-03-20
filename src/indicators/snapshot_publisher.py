from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def store_preview_snapshot(
    manager,
    symbol: str,
    timeframe: str,
    bar_time: datetime,
    indicators: Dict[str, Dict[str, float]],
) -> bool:
    cache_key = f"{symbol}_{timeframe}"
    normalized = {
        name: dict(payload)
        for name, payload in indicators.items()
    }
    current = manager._last_preview_snapshot.get(cache_key)
    if current is not None and current[0] == bar_time and current[1] == normalized:
        return False
    manager._last_preview_snapshot.pop(cache_key, None)
    manager._last_preview_snapshot[cache_key] = (bar_time, normalized)
    max_entries = getattr(manager, "_preview_snapshot_max_entries", 500)
    while len(manager._last_preview_snapshot) > max_entries:
        manager._last_preview_snapshot.popitem(last=False)
    return True


def publish_snapshot(
    manager,
    symbol: str,
    timeframe: str,
    bar_time: datetime,
    indicators: Dict[str, Dict[str, float]],
    *,
    scope: str,
) -> None:
    lock = getattr(manager, "_snapshot_listeners_lock", None)
    if lock is None:
        listeners = list(getattr(manager, "_snapshot_listeners", []))
    else:
        with lock:
            listeners = list(getattr(manager, "_snapshot_listeners", []))
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


def publish_intrabar_snapshot(
    manager,
    symbol: str,
    timeframe: str,
    bar_time: datetime,
    indicators: Dict[str, Dict[str, float]],
) -> Dict[str, Dict[str, float]]:
    if not indicators:
        return {}
    if not store_preview_snapshot(manager, symbol, timeframe, bar_time, indicators):
        return indicators
    publish_snapshot(
        manager,
        symbol,
        timeframe,
        bar_time,
        indicators,
        scope="intrabar",
    )
    return indicators


def write_back_results(
    manager,
    symbol: str,
    timeframe: str,
    bars: List[Any],
    results: Dict[str, Dict[str, Any]],
    compute_time_ms: float,
    bar_time: Optional[datetime] = None,
) -> Dict[str, Dict[str, float]]:
    effective_bar_time = bar_time or (bars[-1].time if bars else None)
    manager._store_results(symbol, timeframe, effective_bar_time, results, compute_time_ms)

    if not bars or effective_bar_time is None:
        return {}

    grouped = manager._group_indicator_values(results)
    if not grouped:
        return {}

    latest_bar = bars[-1]
    manager.market_service.update_ohlc_indicators(
        symbol,
        timeframe,
        effective_bar_time,
        grouped,
    )

    if manager.storage_writer is not None:
        row = (
            latest_bar.symbol,
            latest_bar.timeframe,
            latest_bar.open,
            latest_bar.high,
            latest_bar.low,
            latest_bar.close,
            latest_bar.volume,
            latest_bar.time.isoformat(),
            dict(getattr(latest_bar, "indicators", {}) or {}),
        )
        manager.storage_writer.enqueue("ohlc_indicators", row)

    publish_snapshot(
        manager,
        symbol,
        timeframe,
        effective_bar_time,
        grouped,
        scope="confirmed",
    )

    return grouped
