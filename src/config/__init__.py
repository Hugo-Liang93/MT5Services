"""
配置系统 - 支持集中式配置管理（单一信号源）
提供向后兼容的配置加载接口。
"""

from __future__ import annotations

import configparser

# Primary runtime imports should come from this module. The compatibility
# loaders re-exported below are kept only for older call sites.

# 导出兼容层接口（保持现有代码不变）
from src.config.compat import (
    # 配置类
    MT5Settings,
    DBSettings,
    CompatIngestSettings as IngestSettings,
    CompatMarketSettings as MarketSettings,
    StorageSettings,
    IndicatorSettings,
    
    # 加载函数
    load_mt5_settings,
    load_mt5_accounts,
    load_db_settings,
    load_ingest_settings,
    load_storage_settings,
    load_market_settings,
    load_indicator_settings,
    load_indicator_tasks,
)

from src.config.indicator_config import (
    CacheStrategy,
    ComputeMode,
    ConfigLoader as IndicatorConfigLoader,
    ConfigManager as IndicatorConfigManager,
    IndicatorConfig,
    PipelineConfig as IndicatorPipelineConfig,
    UnifiedIndicatorConfig,
    get_config as get_indicator_config,
    get_global_config_manager as get_indicator_config_manager,
    normalize_indicator_func_path,
)

# 导出集中式配置系统
from src.config.centralized import (
    # 配置类
    TradingConfig,
    IntervalConfig,
    LimitConfig,
    SystemConfig,
    APIConfig,
    IngestConfig,
    EconomicConfig,
    RiskConfig,
    
    # 加载函数
    get_trading_config,
    get_interval_config,
    get_limit_config,
    get_system_config,
    get_api_config,
    get_ingest_config,
    get_economic_config,
    get_risk_config,
    
    # 工具函数
    get_shared_symbols,
    get_shared_timeframes,
    get_shared_default_symbol,
    get_effective_config_snapshot,
    get_config_provenance_snapshot,
    validate_config_consistency,
    reload_configs,
)

# 导出工具函数
from src.config.utils import (
    resolve_config_path,
    load_ini_config,
    load_config_with_base,
    get_merged_option_source,
    get_merged_config,
    ConfigValidator,
)


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


def get_runtime_ingest_settings() -> IngestSettings:
    trading = get_trading_config()
    intervals = get_interval_config()
    limits = get_limit_config()
    ingest = get_ingest_config()
    cache = _get_cache_section()

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
    )


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

__all__ = [
    # 兼容层
    "MT5Settings",
    "DBSettings",
    "IngestSettings",
    "MarketSettings",
    "StorageSettings",
    "IndicatorSettings",
    "load_mt5_settings",
    "load_mt5_accounts",
    "load_db_settings",
    "load_ingest_settings",
    "load_storage_settings",
    "load_market_settings",
    "load_indicator_settings",
    "load_indicator_tasks",
    "CacheStrategy",
    "ComputeMode",
    "IndicatorConfigLoader",
    "IndicatorConfigManager",
    "IndicatorConfig",
    "IndicatorPipelineConfig",
    "UnifiedIndicatorConfig",
    "get_indicator_config",
    "get_indicator_config_manager",
    "normalize_indicator_func_path",
    
    # 集中式配置
    "TradingConfig",
    "IntervalConfig",
    "LimitConfig",
    "SystemConfig",
    "APIConfig",
    "IngestConfig",
    "EconomicConfig",
    "RiskConfig",
    "get_trading_config",
    "get_interval_config",
    "get_limit_config",
    "get_system_config",
    "get_api_config",
    "get_ingest_config",
    "get_economic_config",
    "get_risk_config",
    "get_runtime_ingest_settings",
    "get_runtime_market_settings",
    "get_shared_symbols",
    "get_shared_timeframes",
    "get_shared_default_symbol",
    "get_effective_config_snapshot",
    "get_config_provenance_snapshot",
    "validate_config_consistency",
    "reload_configs",
    
    # 工具函数
    "resolve_config_path",
    "load_ini_config",
    "load_config_with_base",
    "get_merged_option_source",
    "get_merged_config",
    "ConfigValidator",
]
