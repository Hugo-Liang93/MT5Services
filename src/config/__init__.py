"""
按模块拆分的配置加载：仅从 config/*.ini 读取，不依赖环境变量。
提供 MT5、DB、Ingest、Storage、Indicator 五类配置的模型与加载函数。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional, List
import configparser
import json
import os

from pydantic import BaseModel, Field

from src.config.utils import resolve_config_path
from src.indicators.types import IndicatorTask


class MT5Settings(BaseModel):
    mt5_login: Optional[int] = None
    mt5_password: Optional[str] = None
    mt5_server: Optional[str] = None
    mt5_path: Optional[str] = None
    timezone: str = "UTC"


class DBSettings(BaseModel):
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "postgres"
    pg_password: str = "postgres"
    pg_database: str = "mt5"
    pg_schema: str = "public"


class IngestSettings(BaseModel):
    tick_cache_size: int = 5000
    tick_initial_lookback_seconds: int = 20
    ingest_symbols: List[str] = Field(default_factory=lambda: ["EURUSD"])
    ingest_tick_interval: float = 0.5
    ingest_ohlc_timeframes: List[str] = Field(default_factory=lambda: ["M1", "H1"])
    ingest_ohlc_interval: float = 30.0
    ingest_ohlc_intervals: dict = Field(default_factory=dict)
    ohlc_backfill_limit: int = 500


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


class StorageSettings(BaseModel):
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
    poll_seconds: float = 5.0
    reload_interval: float = 60.0
    config_path: Optional[str] = None
    backfill_enabled: bool = True
    backfill_batch_size: int = 1000


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


@lru_cache
def load_ingest_settings() -> IngestSettings:
    sec = _load_ini_section("ingest.ini", "ingest")
    cache_sec = _load_ini_section("cache.ini", "cache")
    return IngestSettings(
        tick_cache_size=_cfg_int(cache_sec, "tick_cache_size", 5000),
        tick_initial_lookback_seconds=_cfg_int(sec, "tick_initial_lookback_seconds", 20),
        ingest_symbols=_cfg_list(sec, "ingest_symbols", ["EURUSD"]),
        ingest_tick_interval=_cfg_float(sec, "ingest_tick_interval", 0.5),
        ingest_ohlc_timeframes=_cfg_list(sec, "ingest_ohlc_timeframes", ["M1", "H1"]),
        ingest_ohlc_interval=_cfg_float(sec, "ingest_ohlc_interval", 30.0),
        ingest_ohlc_intervals=_cfg_map(sec, "ingest_ohlc_intervals", {}),
        ohlc_backfill_limit=_cfg_int(sec, "ohlc_backfill_limit", 500),
    )


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


@lru_cache
def load_market_settings() -> MarketSettings:
    sec = _load_ini_section("market.ini", "market")
    cache_sec = _load_ini_section("cache.ini", "cache")
    return MarketSettings(
        default_symbol=_cfg_str(sec, "default_symbol", "EURUSD"),
        tick_limit=_cfg_int(sec, "tick_limit", 100),
        ohlc_limit=_cfg_int(sec, "ohlc_limit", 100),
        quote_stale_seconds=_cfg_float(sec, "quote_stale_seconds", 1.0),
        stream_interval_seconds=_cfg_float(sec, "stream_interval_seconds", 1.0),
        tick_cache_size=_cfg_int(cache_sec, "tick_cache_size", 5000),
        ohlc_cache_limit=_cfg_int(cache_sec, "ohlc_cache_limit", 500),
        intrabar_max_points=_cfg_int(cache_sec, "intrabar_max_points", 500),
        ohlc_event_queue_size=_cfg_int(cache_sec, "ohlc_event_queue_size", 1000),
    )


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


def _load_ini_section(filename: str, section: str) -> Optional[configparser.SectionProxy]:
    path = resolve_config_path(filename)
    if not path or not os.path.exists(path):
        return None
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    return parser[section] if parser.has_section(section) else None


def _cfg_str(sec: Optional[configparser.SectionProxy], key: str, default=None):
    return sec.get(key, fallback=default) if sec else default


def _cfg_int(sec: Optional[configparser.SectionProxy], key: str, default=None):
    if sec is None:
        return default
    try:
        return sec.getint(key, fallback=default)
    except Exception:
        return default


def _cfg_float(sec: Optional[configparser.SectionProxy], key: str, default=None):
    if sec is None:
        return default
    try:
        return sec.getfloat(key, fallback=default)
    except Exception:
        return default


def _cfg_bool(sec: Optional[configparser.SectionProxy], key: str, default: bool):
    if sec is None:
        return default
    val = sec.get(key, fallback=None)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _cfg_list(sec: Optional[configparser.SectionProxy], key: str, default):
    if sec is None:
        return default
    raw = sec.get(key, fallback=None)
    if raw is None:
        return default
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


def _cfg_map(sec: Optional[configparser.SectionProxy], key: str, default: dict):
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
