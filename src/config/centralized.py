"""
Centralized configuration manager.

This module provides a single source of truth across trading, ingest, and API layers
while keeping backward-compatible loader functions for existing callers.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from src.config.utils import ConfigValidator, get_merged_config


class TradingConfig(BaseModel):
    symbols: List[str] = Field(default_factory=lambda: ["XAUUSD"])
    timeframes: List[str] = Field(default_factory=lambda: ["M1", "H1"])
    default_symbol: str = "XAUUSD"


class IntervalConfig(BaseModel):
    tick_interval: float = 0.5
    ohlc_interval: float = 30.0
    stream_interval: float = 1.0
    indicator_reload_interval: float = 60.0


class LimitConfig(BaseModel):
    tick_limit: int = 200
    ohlc_limit: int = 200
    tick_cache_size: int = 5000
    ohlc_cache_limit: int = 500
    quote_stale_seconds: float = 1.0


class SystemConfig(BaseModel):
    timezone: str = "UTC"
    log_level: str = "INFO"
    modules_enabled: List[str] = Field(default_factory=lambda: ["ingest", "api", "indicators", "storage"])


class APIConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8808
    enable_cors: bool = True
    docs_enabled: bool = True
    redoc_enabled: bool = True
    auth_enabled: bool = False
    api_key_header: str = "X-API-Key"
    access_log_enabled: bool = True
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class IngestConfig(BaseModel):
    tick_initial_lookback_seconds: int = 20
    ohlc_backfill_limit: int = 500
    retry_attempts: int = 3
    retry_backoff: float = 1.0
    connection_timeout: float = 10.0
    max_concurrent_symbols: int = 5
    queue_monitor_interval: float = 5.0
    health_check_interval: float = 30.0
    max_allowed_delay: float = 60.0


def _split_csv(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


class CentralizedConfig:
    def __init__(self):
        self._config_cache: Dict[str, Any] = {}
        self._validated = False

    def load_all(self) -> Dict[str, Any]:
        if not self._config_cache:
            self._load_and_validate()
        return self._config_cache

    def _load_and_validate(self):
        main_config = get_merged_config("app.ini")
        configs = {
            "main": main_config,
            "api": get_merged_config("market.ini"),
            "ingest": get_merged_config("ingest.ini"),
            "mt5": get_merged_config("mt5.ini"),
            "db": get_merged_config("db.ini"),
            "storage": get_merged_config("storage.ini"),
            "cache": get_merged_config("cache.ini"),
            "indicators": get_merged_config("indicators.ini"),
        }

        shared_config = self._extract_shared_config(main_config)
        self._validate_configs(shared_config, configs)
        self._build_final_config(shared_config, configs)
        self._validated = True

    def _extract_shared_config(self, main_config: Dict[str, Any]) -> Dict[str, Any]:
        trading_raw = dict(main_config.get("trading", {}))
        system_raw = dict(main_config.get("system", {}))

        if "symbols" in trading_raw:
            trading_raw["symbols"] = _split_csv(trading_raw.get("symbols"))
        if "timeframes" in trading_raw:
            trading_raw["timeframes"] = _split_csv(trading_raw.get("timeframes"))
        if "modules_enabled" in system_raw:
            system_raw["modules_enabled"] = _split_csv(system_raw.get("modules_enabled"))

        return {
            "trading": TradingConfig(**trading_raw).model_dump(),
            "intervals": IntervalConfig(**main_config.get("intervals", {})).model_dump(),
            "limits": LimitConfig(**main_config.get("limits", {})).model_dump(),
            "system": SystemConfig(**system_raw).model_dump(),
        }

    def _validate_configs(self, shared_config: Dict[str, Any], configs: Dict[str, Dict[str, Any]]):
        ConfigValidator.validate_trading_config(shared_config)
        ConfigValidator.validate_interval_config(shared_config)
        self._check_config_inheritance(shared_config, configs)

    def _check_config_inheritance(self, shared_config: Dict[str, Any], configs: Dict[str, Dict[str, Any]]):
        trading_symbols = shared_config["trading"]["symbols"]
        api_config = configs["api"]
        if "default_symbol" in api_config.get("api", {}):
            api_default = api_config["api"]["default_symbol"]
            if api_default not in trading_symbols:
                raise ValueError(
                    f"API default symbol '{api_default}' not in trading symbols: {trading_symbols}"
                )

    def _build_final_config(self, shared_config: Dict[str, Any], configs: Dict[str, Dict[str, Any]]):
        self._config_cache = {
            **shared_config,
            "api": APIConfig(**configs["api"].get("api", {})).model_dump(),
            "ingest": IngestConfig(**configs["ingest"].get("ingest", {})).model_dump(),
            "raw": {
                "mt5": configs["mt5"],
                "db": configs["db"],
                "storage": configs["storage"],
                "cache": configs["cache"],
                "indicators": configs["indicators"],
            },
        }

    def get_trading_config(self) -> TradingConfig:
        return TradingConfig(**self.load_all()["trading"])

    def get_interval_config(self) -> IntervalConfig:
        return IntervalConfig(**self.load_all()["intervals"])

    def get_limit_config(self) -> LimitConfig:
        return LimitConfig(**self.load_all()["limits"])

    def get_api_config(self) -> APIConfig:
        return APIConfig(**self.load_all()["api"])

    def get_ingest_config(self) -> IngestConfig:
        return IngestConfig(**self.load_all()["ingest"])

    def get_system_config(self) -> SystemConfig:
        return SystemConfig(**self.load_all()["system"])

    def get_raw_config(self, module: str) -> Dict[str, Any]:
        return self.load_all()["raw"].get(module, {})

    def reload(self):
        self._config_cache.clear()
        self._validated = False
        self.load_all()


_config_manager = CentralizedConfig()


@lru_cache
def get_trading_config() -> TradingConfig:
    return _config_manager.get_trading_config()


@lru_cache
def get_interval_config() -> IntervalConfig:
    return _config_manager.get_interval_config()


@lru_cache
def get_limit_config() -> LimitConfig:
    return _config_manager.get_limit_config()


@lru_cache
def get_api_config() -> APIConfig:
    return _config_manager.get_api_config()


@lru_cache
def get_ingest_config() -> IngestConfig:
    return _config_manager.get_ingest_config()


@lru_cache
def get_system_config() -> SystemConfig:
    return _config_manager.get_system_config()


def reload_configs():
    get_trading_config.cache_clear()
    get_interval_config.cache_clear()
    get_limit_config.cache_clear()
    get_api_config.cache_clear()
    get_ingest_config.cache_clear()
    get_system_config.cache_clear()
    _config_manager.reload()


def get_shared_symbols() -> List[str]:
    return get_trading_config().symbols


def get_shared_timeframes() -> List[str]:
    return get_trading_config().timeframes


def get_shared_default_symbol() -> str:
    return get_trading_config().default_symbol


def validate_config_consistency():
    try:
        _config_manager.load_all()
        return True, "config validation passed"
    except Exception as e:
        return False, f"config validation failed: {e}"
