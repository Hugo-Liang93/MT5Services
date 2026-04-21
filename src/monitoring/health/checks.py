from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict

from src.utils.common import timeframe_seconds

logger = logging.getLogger(__name__)


def check_data_latency(
    monitor,
    component: str,
    service: Any,
    symbol: str,
    timeframe: str,
) -> float:
    try:
        latest_bar = service.get_latest_ohlc(symbol, timeframe)
        if not latest_bar:
            latency = float("inf")
            interval_seconds = None
            next_expected_close = None
        else:
            bar_open_time = monitor._as_utc(latest_bar.time)
            interval_seconds = max(1, timeframe_seconds(timeframe))
            next_expected_close = bar_open_time + timedelta(
                seconds=interval_seconds * 2
            )
            latency = max(
                0.0, (monitor._utc_now() - next_expected_close).total_seconds()
            )

        # 休市感知：若 K 线延迟过大，检查是否有近期 live quote。
        # 休市/夜盘期间 MT5 不推送新 K 线，但报价仍在更新，此时不应触发 CRITICAL 告警。
        # 1 小时内有 live quote → 视为连接正常，最多报告 60 秒延迟（不触发 critical）。
        live_latency = latency
        if latency > 120:
            try:
                quote_getter = getattr(service, "get_latest_quote", None) or getattr(
                    service, "get_quote", None
                )
                if quote_getter is not None:
                    quote = quote_getter(symbol)
                    if quote is not None:
                        q_time = getattr(quote, "time", None)
                        if q_time is not None:
                            q_age = (
                                monitor._utc_now() - monitor._as_utc(q_time)
                            ).total_seconds()
                            if q_age < 3600:
                                # 有近期报价 → 连接正常，延迟上限 25s（低于 critical 阈值 30s，避免误报）
                                live_latency = min(latency, 25.0)
            except Exception:
                pass

        monitor.record_metric(
            component,
            "data_latency",
            live_latency,
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "latest_bar_time": latest_bar.time.isoformat() if latest_bar else None,
                "interval_seconds": interval_seconds,
                "next_expected_close": (
                    next_expected_close.isoformat() if next_expected_close else None
                ),
                "bar_latency_seconds": latency,
                "live_quote_adjusted": live_latency < latency,
            },
        )
        return live_latency
    except Exception as exc:
        logger.error(
            "Failed to check data latency for %s/%s: %s", symbol, timeframe, exc
        )
        return float("inf")


def check_indicator_freshness(
    monitor,
    component: str,
    worker: Any,
    symbol: str,
    timeframe: str,
) -> float:
    try:
        snapshot = worker.get_snapshot(symbol, timeframe)
        if not snapshot:
            freshness = float("inf")
        else:
            if hasattr(snapshot, "timestamp"):
                snapshot_time = snapshot.timestamp
            elif hasattr(snapshot, "bar_time"):
                snapshot_time = snapshot.bar_time
            else:
                snapshot_time = monitor._utc_now() - timedelta(days=1)
            freshness = (
                monitor._utc_now() - monitor._as_utc(snapshot_time)
            ).total_seconds()

        monitor.record_metric(
            component,
            "indicator_freshness",
            freshness,
            {"symbol": symbol, "timeframe": timeframe},
        )
        return freshness
    except Exception as exc:
        logger.error(
            "Failed to check indicator freshness for %s/%s: %s",
            symbol,
            timeframe,
            exc,
        )
        return float("inf")


def check_queue_stats(monitor, component: str, ingestor: Any) -> Dict[str, Any]:
    try:
        stats = ingestor.queue_stats()
        for queue_name, queue_info in stats.get("queues", {}).items():
            depth = queue_info.get("size", 0) + queue_info.get("pending", 0)
            monitor.record_metric(
                component,
                "queue_depth",
                depth,
                {"queue_name": queue_name, "stats": queue_info},
            )
        return stats
    except Exception as exc:
        logger.error("Failed to check queue stats: %s", exc)
        return {}


def _collect_worker_stats(worker: Any) -> Dict[str, Any]:
    """调用 worker.get_performance_stats() 或 .stats()。

    对 `RuntimeError: dictionary changed size during iteration` 做 1 次重试：
    indicator manager 内部有 50+ 个 dict 在 hot path 被并发 mutate，个别罕见
    快照路径可能与写路径错开一个窗口。race 本身是瞬时的，再采一次通常就稳。
    不无限重试——防止 worker 长期故障时打满监控线程。
    """
    getter = (
        worker.get_performance_stats
        if hasattr(worker, "get_performance_stats")
        else worker.stats
    )
    try:
        return getter()
    except RuntimeError as exc:
        if "dictionary changed size" not in str(exc):
            raise
        # 瞬时 race——一次重试
        return getter()


def check_cache_stats(monitor, component: str, worker: Any) -> Dict[str, Any]:
    try:
        stats = _collect_worker_stats(worker)
        cache_hits = stats.get("cache_hits", 0)
        cache_misses = stats.get("cache_misses", 0)
        total = cache_hits + cache_misses
        if total > 0:
            monitor.record_metric(
                component,
                "cache_hit_rate",
                cache_hits / total,
                {"hits": cache_hits, "misses": cache_misses, "total": total},
            )
        return stats
    except RuntimeError as exc:
        # 重试后仍 race——降级为 DEBUG，不污染 errors.log
        if "dictionary changed size" in str(exc):
            logger.debug("check_cache_stats race persisted: %s", exc)
            return {}
        logger.error("Failed to check cache stats: %s", exc)
        return {}
    except Exception as exc:
        logger.error("Failed to check cache stats: %s", exc)
        return {}


def check_economic_calendar(monitor, component: str, service: Any) -> Dict[str, Any]:
    try:
        stats = service.stats()
        warming_up = str(stats.get("warming_up", "false")).lower() == "true"
        staleness_raw = stats.get("staleness_seconds")
        if staleness_raw is None:
            stale_seconds = 0.0 if warming_up else float("inf")
        else:
            stale_seconds = float(staleness_raw)

        provider_status = stats.get("provider_status") or {}
        if isinstance(provider_status, dict):
            provider_failures = float(
                sum(
                    int(
                        (provider_status.get(name) or {}).get("consecutive_failures", 0)
                    )
                    for name in provider_status
                )
            )
        else:
            provider_failures = 0.0

        monitor.record_metric(
            component,
            "economic_calendar_staleness",
            stale_seconds,
            {"stats": stats},
        )
        monitor.record_metric(
            component,
            "economic_provider_failures",
            provider_failures,
            {"provider_status": provider_status},
        )
        return stats
    except Exception as exc:
        logger.error("Failed to check economic calendar health: %s", exc)
        return {}
