from __future__ import annotations

import configparser
from functools import lru_cache

from pydantic import BaseModel, Field

from src.config.centralized import (
    get_ingest_config,
    get_interval_config,
    get_limit_config,
    get_trading_config,
)
from src.config.storage import load_storage_settings
from src.config.utils import load_config_with_base


class IngestSettings(BaseModel):
    tick_cache_size: int = 5000
    tick_initial_lookback_seconds: int = 20
    ingest_symbols: list[str] = Field(default_factory=lambda: ["EURUSD"])
    ingest_tick_interval: float = 0.5
    ingest_ohlc_timeframes: list[str] = Field(default_factory=lambda: ["M1", "H1"])
    ingest_ohlc_interval: float = 30.0
    ingest_ohlc_intervals: dict = Field(default_factory=dict)
    ohlc_backfill_limit: int = 500
    retry_attempts: int = 3
    retry_backoff: float = 1.0
    connection_timeout: float = 10.0
    max_concurrent_symbols: int = 5
    queue_monitor_interval: float = 5.0
    health_check_interval: float = 30.0
    max_allowed_delay: float = 60.0
    intrabar_enabled: bool = True
    ingest_intrabar_interval: float = 15.0
    ingest_intrabar_intervals: dict = Field(default_factory=dict)


class MarketSettings(BaseModel):
    default_symbol: str = "EURUSD"
    tick_limit: int = 100
    ohlc_limit: int = 100
    quote_stale_seconds: float = 1.0
    stream_interval_seconds: float = 1.0
    tick_cache_size: int = 5000
    ohlc_cache_limit: int = 500
    intrabar_max_points: int = 500
    ohlc_event_queue_size: int = 1000


def _get_cache_section() -> configparser.SectionProxy | None:
    _, parser = load_config_with_base("cache.ini")
    if not parser or not parser.has_section("cache"):
        return None
    return parser["cache"]


def _cache_int(section: configparser.SectionProxy | None, key: str, default: int) -> int:
    if section is None:
        return default
    try:
        return section.getint(key, fallback=default)
    except Exception:
        return default


@lru_cache
def get_runtime_ingest_settings() -> IngestSettings:
    trading = get_trading_config()
    intervals = get_interval_config()
    limits = get_limit_config()
    ingest = get_ingest_config()
    cache = _get_cache_section()
    storage = load_storage_settings()

    return IngestSettings(
        tick_cache_size=_cache_int(cache, "tick_cache_size", limits.tick_cache_size),
        tick_initial_lookback_seconds=ingest.tick_initial_lookback_seconds,
        ingest_symbols=trading.symbols,
        ingest_tick_interval=intervals.tick_interval,
        ingest_ohlc_timeframes=trading.timeframes,
        ingest_ohlc_interval=intervals.ohlc_interval,
        ingest_ohlc_intervals={},
        ohlc_backfill_limit=ingest.ohlc_backfill_limit,
        retry_attempts=ingest.retry_attempts,
        retry_backoff=ingest.retry_backoff,
        connection_timeout=ingest.connection_timeout,
        max_concurrent_symbols=ingest.max_concurrent_symbols,
        queue_monitor_interval=ingest.queue_monitor_interval,
        health_check_interval=ingest.health_check_interval,
        max_allowed_delay=ingest.max_allowed_delay,
        intrabar_enabled=storage.intrabar_enabled,
        ingest_intrabar_interval=ingest.intrabar_interval,
        ingest_intrabar_intervals=dict(ingest.intrabar_intervals),
    )


@lru_cache
def get_runtime_market_settings() -> MarketSettings:
    trading = get_trading_config()
    intervals = get_interval_config()
    limits = get_limit_config()
    cache = _get_cache_section()

    return MarketSettings(
        default_symbol=trading.default_symbol,
        tick_limit=limits.tick_limit,
        ohlc_limit=limits.ohlc_limit,
        quote_stale_seconds=limits.quote_stale_seconds,
        stream_interval_seconds=intervals.stream_interval,
        tick_cache_size=_cache_int(cache, "tick_cache_size", limits.tick_cache_size),
        ohlc_cache_limit=_cache_int(cache, "ohlc_cache_limit", limits.ohlc_cache_limit),
        intrabar_max_points=_cache_int(cache, "intrabar_max_points", 500),
        ohlc_event_queue_size=_cache_int(cache, "ohlc_event_queue_size", 1000),
    )
