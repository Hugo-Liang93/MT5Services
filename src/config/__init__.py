"""
配置系统 - 支持集中式配置管理（单一信号源）
提供向后兼容的配置加载接口。
"""

from __future__ import annotations

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
    
    # 加载函数
    get_trading_config,
    get_interval_config,
    get_limit_config,
    get_system_config,
    get_api_config,
    get_ingest_config,
    
    # 工具函数
    get_shared_symbols,
    get_shared_timeframes,
    get_shared_default_symbol,
    validate_config_consistency,
    reload_configs,
)

# 导出工具函数
from src.config.utils import (
    resolve_config_path,
    load_ini_config,
    load_config_with_base,
    get_merged_config,
    ConfigValidator,
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
    "get_trading_config",
    "get_interval_config",
    "get_limit_config",
    "get_system_config",
    "get_api_config",
    "get_ingest_config",
    "get_shared_symbols",
    "get_shared_timeframes",
    "get_shared_default_symbol",
    "validate_config_consistency",
    "reload_configs",
    
    # 工具函数
    "resolve_config_path",
    "load_ini_config",
    "load_config_with_base",
    "get_merged_config",
    "ConfigValidator",
]
