"""SignalRuntime snapshot metadata 组装。

从 runtime.py 的 _on_snapshot() 提取：
- trace_id 解析
- spread / symbol_point 获取
- bar_progress 计算（intrabar scope）

纯函数，不依赖 runtime 内部状态。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from src.utils.common import timeframe_seconds

from ..metadata_keys import MetadataKey as MK

logger = logging.getLogger(__name__)


def build_snapshot_metadata(
    *,
    scope: str,
    symbol: str,
    timeframe: str,
    bar_time: datetime,
    snapshot_source: Any,
    trace_id: str,
) -> dict[str, Any]:
    """为 indicator snapshot 组装完整 metadata dict。"""
    metadata: dict[str, Any] = {
        MK.SCOPE: scope,
        MK.BAR_TIME: bar_time.isoformat(),
        MK.SNAPSHOT_TIME: datetime.now(timezone.utc).isoformat(),
        "trigger_source": f"{scope}_snapshot",
        MK.SIGNAL_TRACE_ID: trace_id,
    }

    _inject_spread(metadata, symbol, snapshot_source)

    if scope == "intrabar":
        _inject_bar_progress(metadata, bar_time, timeframe)

    return metadata


def _inject_spread(
    metadata: dict[str, Any],
    symbol: str,
    snapshot_source: Any,
) -> None:
    """尝试获取 spread 和 symbol point 并注入 metadata。"""
    spread_getter = getattr(snapshot_source, "get_current_spread", None)
    market_service = getattr(snapshot_source, "market_service", None)
    if not callable(spread_getter) and market_service is not None:
        spread_getter = getattr(market_service, "get_current_spread", None)

    point_getter = getattr(snapshot_source, "get_symbol_point", None)
    if not callable(point_getter) and market_service is not None:
        point_getter = getattr(market_service, "get_symbol_point", None)

    if not callable(spread_getter):
        return

    try:
        spread_points = float(spread_getter(symbol))
        metadata[MK.SPREAD_POINTS] = spread_points
        if callable(point_getter):
            point_size = float(point_getter(symbol))
            metadata[MK.SYMBOL_POINT] = point_size
            metadata[MK.SPREAD_PRICE] = spread_points * point_size
    except (TypeError, ValueError, AttributeError, KeyError):
        logger.debug(
            "Failed to resolve spread/point for %s",
            symbol,
            exc_info=True,
        )


def _inject_bar_progress(
    metadata: dict[str, Any],
    bar_time: datetime,
    timeframe: str,
) -> None:
    """计算 intrabar bar_progress 并注入 metadata。"""
    if bar_time.tzinfo is None:
        bar_time = bar_time.replace(tzinfo=timezone.utc)
    elapsed = (
        datetime.now(timezone.utc) - bar_time.astimezone(timezone.utc)
    ).total_seconds()
    metadata[MK.BAR_PROGRESS] = max(
        0.0, min(elapsed / max(timeframe_seconds(timeframe), 1), 1.0)
    )
