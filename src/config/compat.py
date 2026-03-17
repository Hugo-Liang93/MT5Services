"""
配置兼容性层 - 逐步迁移到集中式配置
"""

from __future__ import annotations

from functools import lru_cache
from typing import Dict, List, Optional
import os

# Thin compatibility layer. New runtime code should prefer src.config getters
# and runtime settings factories instead of importing from this module.

from pydantic import BaseModel, Field

from src.config.utils import load_config_with_base, resolve_config_path
from src.config.centralized import (
    get_trading_config, get_interval_config, get_limit_config,
    get_ingest_config,
)
from src.indicators.types import IndicatorTask
from src.config.indicator_config import normalize_indicator_func_path


class MT5Settings(BaseModel):
    """MT5设置（保持原样）"""
    account_alias: str = "default"
    account_label: Optional[str] = None
    mt5_login: Optional[int] = None
    mt5_password: Optional[str] = None
    mt5_server: Optional[str] = None
    mt5_path: Optional[str] = None
    timezone: str = "UTC"
    server_time_offset_hours: Optional[int] = None
    enabled: bool = True


class DBSettings(BaseModel):
    """数据库设置（保持原样）"""
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "postgres"
    pg_password: str = "postgres"
    pg_database: str = "mt5"
    pg_schema: str = "public"


class CompatIngestSettings(BaseModel):
    """兼容的数据采集设置"""
    tick_cache_size: int = 5000
    tick_initial_lookback_seconds: int = 20
    ingest_symbols: List[str] = Field(default_factory=lambda: ["EURUSD"])
    ingest_tick_interval: float = 0.5
    ingest_ohlc_timeframes: List[str] = Field(default_factory=lambda: ["M1", "H1"])
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
    # Independent intrabar sampling schedule (separate from OHLC poll).
    ingest_intrabar_interval: float = 15.0
    ingest_intrabar_intervals: dict = Field(default_factory=dict)


class CompatMarketSettings(BaseModel):
    """兼容的市场设置"""
    default_symbol: str = "EURUSD"
    tick_limit: int = 100
    ohlc_limit: int = 100
    quote_stale_seconds: float = 1.0
    stream_interval_seconds: float = 1.0
    tick_cache_size: int = 5000
    ohlc_cache_limit: int = 500
    intrabar_max_points: int = 500
    ohlc_event_queue_size: int = 1000


class StorageSettings(BaseModel):
    """存储设置（保持原样）"""
    tick_flush_interval: float = 1.0
    tick_flush_batch_size: int = 1000
    tick_queue_maxsize: int = 20000
    tick_page_size: int = 1000
    quote_flush_enabled: bool = False
    quote_flush_interval: float = 1.0
    quote_flush_batch_size: int = 200
    quote_queue_maxsize: int = 5000
    ohlc_page_size: int = 1000
    ohlc_flush_interval: float = 1.0
    ohlc_flush_batch_size: int = 200
    ohlc_queue_maxsize: int = 5000
    flush_retry_attempts: int = 3
    flush_retry_backoff: float = 1.0
    ohlc_upsert_open_bar: bool = False
    intrabar_enabled: bool = True
    intrabar_flush_interval: float = 5.0
    intrabar_flush_batch_size: int = 200
    intrabar_queue_maxsize: int = 10000
    queue_full_policy: str = "auto"
    queue_put_timeout: float = 0.25


class IndicatorSettings(BaseModel):
    """指标设置（保持原样）"""
    poll_seconds: float = 5.0
    reload_interval: float = 60.0
    config_path: Optional[str] = None
    backfill_enabled: bool = True
    backfill_batch_size: int = 1000


def _build_mt5_settings(base_sec, override_sec=None, *, alias: str = "default") -> MT5Settings:
    def _pick_str(key: str, default=None):
        value = _cfg_str(override_sec, key, None)
        if value not in (None, ""):
            return value
        return _cfg_str(base_sec, key, default)

    def _pick_int(key: str, default=None):
        value = _cfg_int(override_sec, key, None)
        if value is not None:
            return value
        return _cfg_int(base_sec, key, default)

    enabled_value = _cfg_bool(override_sec, "enabled", _cfg_bool(base_sec, "enabled", True))
    return MT5Settings(
        account_alias=alias,
        account_label=_pick_str("label", None),
        mt5_login=_pick_int("login", None),
        mt5_password=_pick_str("password", None),
        mt5_server=_pick_str("server", None),
        mt5_path=_pick_str("path", None),
        timezone=_pick_str("timezone", "UTC"),
        server_time_offset_hours=_pick_int("server_time_offset_hours", None),
        enabled=enabled_value,
    )


# MT5配置（支持多账号）
def load_mt5_accounts() -> Dict[str, MT5Settings]:
    path, parser = load_config_with_base("mt5.ini")
    if not path or not parser:
        return {"default": MT5Settings()}

    base_sec = parser["mt5"] if parser.has_section("mt5") else None
    accounts: Dict[str, MT5Settings] = {}
    default_alias = _cfg_str(base_sec, "default_account", "default") if base_sec is not None else "default"

    for section_name in parser.sections():
        if not section_name.startswith("account."):
            continue
        alias = section_name.split(".", 1)[1].strip()
        if not alias:
            continue
        settings = _build_mt5_settings(base_sec, parser[section_name], alias=alias)
        if settings.enabled:
            accounts[alias] = settings

    base_has_profile = any(
        value not in (None, "")
        for value in (
            _cfg_int(base_sec, "login", None),
            _cfg_str(base_sec, "server", None),
            _cfg_str(base_sec, "path", None),
        )
    )
    if base_has_profile or not accounts or default_alias == "default":
        accounts.setdefault("default", _build_mt5_settings(base_sec, None, alias="default"))

    if default_alias in accounts and default_alias != "default":
        ordered = {default_alias: accounts[default_alias]}
        ordered.update({alias: settings for alias, settings in accounts.items() if alias != default_alias})
        return ordered
    return accounts


@lru_cache
def load_mt5_settings(account_alias: Optional[str] = None) -> MT5Settings:
    accounts = load_mt5_accounts()
    if not accounts:
        return MT5Settings()
    if account_alias is None:
        return next(iter(accounts.values()))
    if account_alias not in accounts:
        raise KeyError(f"MT5 account alias not configured: {account_alias}")
    return accounts[account_alias]


# 数据库配置（保持原样）
@lru_cache
def load_db_settings() -> DBSettings:
    sec = _load_ini_section("db.ini", "db")
    return DBSettings(
        pg_host=_cfg_str(sec, "host", "localhost"),
        pg_port=_cfg_int(sec, "port", 5432),
        pg_user=_cfg_str(sec, "user", "postgres"),
        pg_password=_cfg_str(sec, "password", "postgres"),
        pg_database=_cfg_str(sec, "database", "mt5"),
        pg_schema=_cfg_str(sec, "schema", "public"),
    )


# 数据采集配置（使用集中式配置）
@lru_cache
def load_ingest_settings() -> CompatIngestSettings:
    # 从集中式配置获取共享值
    trading = get_trading_config()
    intervals = get_interval_config()
    limits = get_limit_config()
    ingest = get_ingest_config()
    
    # 加载缓存配置（保持兼容）
    cache_sec = _load_ini_section("cache.ini", "cache")
    
    return CompatIngestSettings(
        tick_cache_size=_cfg_int(cache_sec, "tick_cache_size", limits.tick_cache_size),
        tick_initial_lookback_seconds=ingest.tick_initial_lookback_seconds,
        ingest_symbols=trading.symbols,  # 使用共享配置
        ingest_tick_interval=intervals.tick_interval,  # 使用共享配置
        ingest_ohlc_timeframes=trading.timeframes,  # 使用共享配置
        ingest_ohlc_interval=intervals.ohlc_interval,  # 使用共享配置
        ingest_ohlc_intervals={},  # 空字典，使用共享配置
        ohlc_backfill_limit=ingest.ohlc_backfill_limit,
        retry_attempts=ingest.retry_attempts,
        retry_backoff=ingest.retry_backoff,
        connection_timeout=ingest.connection_timeout,
        max_concurrent_symbols=ingest.max_concurrent_symbols,
        queue_monitor_interval=ingest.queue_monitor_interval,
        health_check_interval=ingest.health_check_interval,
        max_allowed_delay=ingest.max_allowed_delay,
        intrabar_enabled=load_storage_settings().intrabar_enabled,
        ingest_intrabar_interval=ingest.intrabar_interval,
        ingest_intrabar_intervals=dict(ingest.intrabar_intervals),
    )


# 存储配置（保持原样）
@lru_cache
def load_storage_settings() -> StorageSettings:
    sec = _load_ini_section("storage.ini", "storage")
    return StorageSettings(
        tick_flush_interval=_cfg_float(sec, "tick_flush_interval", 1.0),
        tick_flush_batch_size=_cfg_int(sec, "tick_flush_batch_size", 1000),
        tick_queue_maxsize=_cfg_int(sec, "tick_queue_maxsize", 20000),
        tick_page_size=_cfg_int(sec, "tick_page_size", 1000),
        quote_flush_enabled=_cfg_bool(sec, "quote_flush_enabled", False),
        quote_flush_interval=_cfg_float(sec, "quote_flush_interval", 1.0),
        quote_flush_batch_size=_cfg_int(sec, "quote_flush_batch_size", 200),
        quote_queue_maxsize=_cfg_int(sec, "quote_queue_maxsize", 5000),
        ohlc_page_size=_cfg_int(sec, "ohlc_page_size", 1000),
        ohlc_flush_interval=_cfg_float(sec, "ohlc_flush_interval", 1.0),
        ohlc_flush_batch_size=_cfg_int(sec, "ohlc_flush_batch_size", 200),
        ohlc_queue_maxsize=_cfg_int(sec, "ohlc_queue_maxsize", 5000),
        flush_retry_attempts=_cfg_int(sec, "flush_retry_attempts", 3),
        flush_retry_backoff=_cfg_float(sec, "flush_retry_backoff", 1.0),
        ohlc_upsert_open_bar=_cfg_bool(sec, "ohlc_upsert_open_bar", False),
        intrabar_enabled=_cfg_bool(sec, "intrabar_enabled", True),
        intrabar_flush_interval=_cfg_float(sec, "intrabar_flush_interval", 5.0),
        intrabar_flush_batch_size=_cfg_int(sec, "intrabar_flush_batch_size", 200),
        intrabar_queue_maxsize=_cfg_int(sec, "intrabar_queue_maxsize", 10000),
        queue_full_policy=_cfg_str(sec, "queue_full_policy", "auto"),
        queue_put_timeout=_cfg_float(sec, "queue_put_timeout", 0.25),
    )


# 市场配置（使用集中式配置）
@lru_cache
def load_market_settings() -> CompatMarketSettings:
    # 从集中式配置获取共享值
    trading = get_trading_config()
    intervals = get_interval_config()
    limits = get_limit_config()
    
    # 加载缓存配置（保持兼容）
    cache_sec = _load_ini_section("cache.ini", "cache")
    
    return CompatMarketSettings(
        default_symbol=trading.default_symbol,  # 使用共享配置
        tick_limit=limits.tick_limit,  # 使用共享配置
        ohlc_limit=limits.ohlc_limit,  # 使用共享配置
        quote_stale_seconds=limits.quote_stale_seconds,  # 使用共享配置
        stream_interval_seconds=intervals.stream_interval,  # 使用共享配置
        tick_cache_size=_cfg_int(cache_sec, "tick_cache_size", limits.tick_cache_size),
        ohlc_cache_limit=_cfg_int(cache_sec, "ohlc_cache_limit", limits.ohlc_cache_limit),
        intrabar_max_points=_cfg_int(cache_sec, "intrabar_max_points", 500),
        ohlc_event_queue_size=_cfg_int(cache_sec, "ohlc_event_queue_size", 1000),
    )


# 指标配置（保持原样）
@lru_cache
def load_indicator_settings() -> IndicatorSettings:
    from src.config.indicator_config import ConfigLoader

    path = resolve_config_path("indicators.json")
    config = ConfigLoader.load(path) if path else ConfigLoader.load("config/indicators.json")
    return IndicatorSettings(
        poll_seconds=float(config.pipeline.poll_interval),
        reload_interval=float(config.reload_interval),
        config_path=path or "config/indicators.json",
        backfill_enabled=True,
        backfill_batch_size=1000,
    )


def load_indicator_tasks(config_path: Optional[str] = None) -> List[IndicatorTask]:
    from src.config.indicator_config import ConfigLoader

    path = resolve_config_path(config_path or "indicators.json")
    if not path or not os.path.exists(path):
        return []
    config = ConfigLoader.load(path)
    return [
        IndicatorTask(
            name=item.name,
            func_path=normalize_indicator_func_path(item.func_path),
            params=dict(item.params),
        )
        for item in config.indicators
        if item.enabled
    ]


# 工具函数（保持原样）
def _load_ini_section(filename: str, section: str):
    path, parser = load_config_with_base(filename)
    if not path or not parser:
        return None
    return parser[section] if parser.has_section(section) else None


def _cfg_str(sec, key: str, default=None):
    if sec is None:
        return default
    value = sec.get(key, fallback=default)
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
        normalized = normalized[1:-1].strip()
    return normalized


def _cfg_int(sec, key: str, default=None):
    if sec is None:
        return default
    try:
        return sec.getint(key, fallback=default)
    except Exception:
        return default


def _cfg_float(sec, key: str, default=None):
    if sec is None:
        return default
    try:
        return sec.getfloat(key, fallback=default)
    except Exception:
        return default


def _cfg_bool(sec, key: str, default: bool):
    if sec is None:
        return default
    val = sec.get(key, fallback=None)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


