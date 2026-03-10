"""
配置兼容性层 - 逐步迁移到集中式配置
"""

from __future__ import annotations

from functools import lru_cache
from typing import List
import configparser
import json
import os

from pydantic import BaseModel, Field

from src.config.utils import resolve_config_path, load_ini_config
from src.config.centralized import (
    get_trading_config, get_interval_config, get_limit_config,
    get_ingest_config, get_api_config, get_system_config,
    get_shared_symbols, get_shared_timeframes, get_shared_default_symbol
)
from src.indicators.types import IndicatorTask


class MT5Settings(BaseModel):
    """MT5设置（保持原样）"""
    mt5_login: Optional[int] = None
    mt5_password: Optional[str] = None
    mt5_server: Optional[str] = None
    mt5_path: Optional[str] = None
    timezone: str = "UTC"


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


class IndicatorSettings(BaseModel):
    """指标设置（保持原样）"""
    poll_seconds: float = 5.0
    reload_interval: float = 60.0
    config_path: Optional[str] = None
    backfill_enabled: bool = True
    backfill_batch_size: int = 1000


# MT5配置（保持原样）
@lru_cache
def load_mt5_settings() -> MT5Settings:
    sec = _load_ini_section("mt5.ini", "mt5")
    return MT5Settings(
        mt5_login=_cfg_int(sec, "login", None),
        mt5_password=_cfg_str(sec, "password", None),
        mt5_server=_cfg_str(sec, "server", None),
        mt5_path=_cfg_str(sec, "path", None),
        timezone=_cfg_str(sec, "timezone", "UTC"),
    )


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
    )


# 市场配置（使用集中式配置）
@lru_cache
def load_market_settings() -> CompatMarketSettings:
    # 从集中式配置获取共享值
    trading = get_trading_config()
    intervals = get_interval_config()
    limits = get_limit_config()
    api = get_api_config()
    
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
    path = resolve_config_path("indicators.ini")
    parser = configparser.ConfigParser()
    if path:
        parser.read(path, encoding="utf-8")
    sec = parser["worker"] if parser.has_section("worker") else None
    return IndicatorSettings(
        poll_seconds=_cfg_float(sec, "poll_seconds", 5.0),
        reload_interval=_cfg_float(sec, "reload_interval", 60.0),
        config_path=path,
        backfill_enabled=_cfg_bool(sec, "backfill_enabled", True),
        backfill_batch_size=_cfg_int(sec, "backfill_batch_size", 1000),
    )


def load_indicator_tasks(config_path: Optional[str] = None) -> List[IndicatorTask]:
    path = resolve_config_path(config_path or "indicators.ini")
    if not path or not os.path.exists(path):
        return []
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    tasks: List[IndicatorTask] = []
    for section in parser.sections():
        if section.strip().lower() == "worker":
            continue
        func_path = parser.get(section, "func", fallback=None)
        if not func_path:
            continue
        params_raw = parser.get(section, "params", fallback="{}")
        try:
            params = json.loads(params_raw)
            if not isinstance(params, dict):
                params = {}
        except Exception:
            params = {}
        tasks.append(
            IndicatorTask(
                name=section,
                func_path=func_path,
                params=params,
            )
        )
    return tasks


# 工具函数（保持原样）
def _load_ini_section(filename: str, section: str):
    path = resolve_config_path(filename)
    if not path or not os.path.exists(path):
        return None
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    return parser[section] if parser.has_section(section) else None


def _cfg_str(sec, key: str, default=None):
    return sec.get(key, fallback=default) if sec else default


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


def _cfg_list(sec, key: str, default):
    if sec is None:
        return default
    raw = sec.get(key, fallback=None)
    if raw is None:
        return default
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


def _cfg_map(sec, key: str, default: dict):
    if sec is None:
        return default
    raw = sec.get(key, fallback=None)
    if raw is None or not raw.strip():
        return default
    result = {}
    for item in raw.split(","):
        if ":" not in item:
            continue
        tf, val = item.split(":", 1)
        tf = tf.strip().upper()
        try:
            result[tf] = float(val)
        except ValueError:
            continue
    return result if result else default