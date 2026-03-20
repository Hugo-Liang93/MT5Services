from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel

from src.config.utils import load_config_with_base


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
    queue_full_policy: str = "auto"
    queue_put_timeout: float = 0.25


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
