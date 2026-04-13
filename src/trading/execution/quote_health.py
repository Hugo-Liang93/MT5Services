from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def build_execution_quote_health(
    market_service: Any,
    symbol: str | None,
) -> dict[str, Any]:
    """构建执行侧 quote freshness 快照。

    API 层的 ``quote_stale_seconds`` 更偏向展示提示，阈值通常较紧；
    执行侧则需要回答“当前 executor 是否仍拥有可用于下单的有效报价”。
    因此这里统一使用更宽的执行阈值：

    - 至少 3 秒；
    - 同时参考 API stale 阈值与流式刷新间隔的 3 倍。
    """

    if market_service is None or not symbol:
        return {
            "stale": False,
            "age_seconds": None,
            "stale_threshold_seconds": None,
        }

    try:
        quote = market_service.get_quote(symbol)
    except Exception:
        logger.debug("Execution quote health probe failed for %s", symbol, exc_info=True)
        return {
            "stale": True,
            "age_seconds": None,
            "stale_threshold_seconds": None,
        }

    if quote is None:
        return {
            "stale": True,
            "age_seconds": None,
            "stale_threshold_seconds": None,
        }

    quote_time = getattr(quote, "time", None)
    if quote_time is None and isinstance(quote, dict):
        quote_time = quote.get("time")
    if quote_time is None:
        return {
            "stale": True,
            "age_seconds": None,
            "stale_threshold_seconds": None,
        }
    if not isinstance(quote_time, datetime):
        return {
            "stale": False,
            "age_seconds": None,
            "stale_threshold_seconds": None,
        }

    normalized = (
        quote_time
        if quote_time.tzinfo is not None
        else quote_time.replace(tzinfo=timezone.utc)
    )
    settings = getattr(market_service, "market_settings", None)
    api_stale_seconds = float(getattr(settings, "quote_stale_seconds", 1.0) or 1.0)
    stream_interval_seconds = float(
        getattr(settings, "stream_interval_seconds", api_stale_seconds)
        or api_stale_seconds
    )
    execution_stale_seconds = max(
        3.0,
        api_stale_seconds * 3.0,
        stream_interval_seconds * 3.0,
    )
    age_seconds = max(0.0, (datetime.now(timezone.utc) - normalized).total_seconds())
    return {
        "stale": age_seconds > execution_stale_seconds,
        "age_seconds": round(age_seconds, 3),
        "stale_threshold_seconds": round(execution_stale_seconds, 3),
    }


__all__ = ["build_execution_quote_health"]
