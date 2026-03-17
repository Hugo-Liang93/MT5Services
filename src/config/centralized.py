"""
Centralized configuration manager.

This module is the primary runtime source of truth across trading, ingest, API,
economic calendar, and monitoring layers. Compatibility loaders should map back
to this module instead of introducing alternate config paths.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from src.config.utils import (
    ConfigValidator,
    get_merged_config,
    get_merged_option_source,
)


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
    api_host: str = "0.0.0.0"
    api_port: int = 8808
    modules_enabled: List[str] = Field(default_factory=lambda: ["ingest", "api", "indicators", "storage"])


class APIConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8808
    enable_cors: bool = True
    docs_enabled: bool = True
    redoc_enabled: bool = True
    auth_enabled: bool = False
    api_key_header: str = "X-API-Key"
    api_key: str | None = None
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


class EconomicConfig(BaseModel):
    enabled: bool = True
    lookback_days: int = 7
    lookahead_days: int = 14
    request_timeout_seconds: float = 10.0
    local_timezone: str = "UTC"
    refresh_interval_seconds: float = 900.0
    calendar_sync_interval_seconds: float = 21600.0
    near_term_refresh_interval_seconds: float = 900.0
    release_watch_interval_seconds: float = 60.0
    startup_calendar_sync_delay_seconds: float = 180.0
    refresh_jitter_seconds: float = 5.0
    startup_refresh: bool = True
    request_retries: int = 3
    retry_backoff_seconds: float = 1.0
    stale_after_seconds: float = 1800.0
    high_importance_threshold: int = 3
    pre_event_buffer_minutes: int = 30
    post_event_buffer_minutes: int = 30
    near_term_window_hours: int = 72
    release_watch_lookback_minutes: int = 15
    release_watch_lookahead_minutes: int = 120
    default_countries: List[str] = Field(default_factory=list)
    fred_release_whitelist_ids: List[str] = Field(default_factory=list)
    fred_release_whitelist_keywords: List[str] = Field(default_factory=list)
    fred_release_blacklist_keywords: List[str] = Field(default_factory=list)
    curated_sources: List[str] = Field(default_factory=lambda: ["tradingeconomics"])
    curated_countries: List[str] = Field(default_factory=list)
    curated_currencies: List[str] = Field(default_factory=list)
    curated_statuses: List[str] = Field(default_factory=lambda: ["scheduled", "imminent", "pending_release", "released"])
    curated_importance_min: int | None = 2
    curated_include_all_day: bool = False
    trade_guard_enabled: bool = True
    trade_guard_mode: str = "warn_only"
    trade_guard_calendar_health_mode: str = "warn_only"
    trade_guard_lookahead_minutes: int = 180
    trade_guard_lookback_minutes: int = 0
    trade_guard_importance_min: int | None = None
    trade_guard_provider_failure_threshold: int = 3
    tradingeconomics_enabled: bool = True
    tradingeconomics_api_key: str | None = None
    fred_enabled: bool = True
    fred_api_key: str | None = None


class RiskConfig(BaseModel):
    enabled: bool = True
    max_positions_per_symbol: int | None = None
    max_open_positions_total: int | None = None
    max_pending_orders_per_symbol: int | None = None
    max_volume_per_order: float | None = None
    max_volume_per_symbol: float | None = None
    require_sl_for_market_orders: bool = False
    require_tp_or_sl_for_market_orders: bool = False


class TradingOpsConfig(BaseModel):
    dispatch_strict_mode: bool = True
    dispatch_timeout_ms: int = 5000
    daily_summary_recent_limit: int = 1000


def _split_csv(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _merge_sections(*sections: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for section in sections:
        if section:
            merged.update(section)
    return merged


def _normalize_log_format(value: Any) -> str:
    normalized = str(value).strip()
    if normalized.startswith('"') and normalized.endswith('"') and len(normalized) >= 2:
        normalized = normalized[1:-1]
    return normalized.replace("%%", "%")


def _normalize_optional_secret(raw_value: Any) -> str | None:
    text = str(raw_value).strip() if raw_value is not None else ""
    return text or None


class CentralizedConfig:
    def __init__(self):
        self._config_cache: Dict[str, Any] = {}
        self._provenance_cache: Dict[str, Dict[str, str]] = {}
        self._validated = False

    def load_all(self) -> Dict[str, Any]:
        if not self._config_cache:
            self._load_and_validate()
        return self._config_cache

    def _load_and_validate(self):
        self._provenance_cache = {}
        main_config = get_merged_config("app.ini")
        configs = {
            "main": main_config,
            "api": get_merged_config("market.ini"),
            "ingest": get_merged_config("ingest.ini"),
            "economic": get_merged_config("economic.ini"),
            "risk": get_merged_config("risk.ini"),
            "mt5": get_merged_config("mt5.ini"),
            "db": get_merged_config("db.ini"),
            "storage": get_merged_config("storage.ini"),
            "cache": get_merged_config("cache.ini"),
            "indicators": {},
        }

        shared_config = self._extract_shared_config(main_config)
        self._validate_configs(shared_config, configs)
        self._build_final_config(shared_config, configs)
        self._validated = True

    def _option_source(
        self,
        *,
        config_name: str,
        section: str,
        key: str,
        fallback: str = "default",
    ) -> str:
        return get_merged_option_source(config_name, section, key) or fallback

    def _set_provenance(self, section: str, field: str, source: str) -> None:
        self._provenance_cache.setdefault(section, {})[field] = source

    def _extract_shared_config(self, main_config: Dict[str, Any]) -> Dict[str, Any]:
        trading_raw = dict(main_config.get("trading", {}))
        system_raw = dict(main_config.get("system", {}))

        if "symbols" in trading_raw:
            trading_raw["symbols"] = _split_csv(trading_raw.get("symbols"))
        if "timeframes" in trading_raw:
            trading_raw["timeframes"] = _split_csv(trading_raw.get("timeframes"))
        if "modules_enabled" in system_raw:
            system_raw["modules_enabled"] = _split_csv(system_raw.get("modules_enabled"))

        shared = {
            "trading": TradingConfig(**trading_raw).model_dump(),
            "intervals": IntervalConfig(**main_config.get("intervals", {})).model_dump(),
            "limits": LimitConfig(**main_config.get("limits", {})).model_dump(),
            "system": SystemConfig(**system_raw).model_dump(),
        }
        for field in shared["trading"]:
            self._set_provenance("trading", field, self._option_source(config_name="app.ini", section="trading", key=field))
        for field in shared["intervals"]:
            self._set_provenance("intervals", field, self._option_source(config_name="app.ini", section="intervals", key=field))
        for field in shared["limits"]:
            self._set_provenance("limits", field, self._option_source(config_name="app.ini", section="limits", key=field))
        for field in shared["system"]:
            self._set_provenance("system", field, self._option_source(config_name="app.ini", section="system", key=field))
        return shared

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
        api_config = _merge_sections(
            {
                "host": shared_config["system"].get("api_host", "0.0.0.0"),
                "port": shared_config["system"].get("api_port", 8808),
            },
            configs["api"].get("api", {}),
            configs["api"].get("security", {}),
            configs["api"].get("logging", {}),
        )
        if "log_format" in api_config:
            api_config["log_format"] = _normalize_log_format(api_config["log_format"])
        if not str(api_config.get("api_key", "")).strip():
            api_config["api_key"] = None
        ingest_config = _merge_sections(
            configs["ingest"].get("ingest", {}),
            configs["ingest"].get("performance", {}),
            configs["ingest"].get("health", {}),
        )
        economic_config = _merge_sections(
            {"local_timezone": shared_config["system"].get("timezone", "UTC")},
            configs["economic"].get("economic", {}),
            {
                "tradingeconomics_enabled": configs["economic"].get("tradingeconomics", {}).get("enabled"),
                "tradingeconomics_api_key": configs["economic"].get("tradingeconomics", {}).get("api_key"),
                "fred_enabled": configs["economic"].get("fred", {}).get("enabled"),
                "fred_api_key": configs["economic"].get("fred", {}).get("api_key"),
            },
        )
        if "default_countries" in economic_config:
            economic_config["default_countries"] = _split_csv(economic_config["default_countries"])
        for list_key in (
            "fred_release_whitelist_ids",
            "fred_release_whitelist_keywords",
            "fred_release_blacklist_keywords",
            "curated_sources",
            "curated_countries",
            "curated_currencies",
            "curated_statuses",
        ):
            if list_key in economic_config:
                economic_config[list_key] = _split_csv(economic_config[list_key])
        for optional_int_key in ("curated_importance_min", "trade_guard_importance_min"):
            if str(economic_config.get(optional_int_key, "")).strip() == "":
                economic_config[optional_int_key] = None
        if "near_term_refresh_interval_seconds" not in economic_config and "refresh_interval_seconds" in economic_config:
            economic_config["near_term_refresh_interval_seconds"] = economic_config["refresh_interval_seconds"]
        economic_config["tradingeconomics_api_key"] = _normalize_optional_secret(
            economic_config.get("tradingeconomics_api_key")
        )
        economic_config["fred_api_key"] = _normalize_optional_secret(
            economic_config.get("fred_api_key")
        )
        risk_config = dict(configs["risk"].get("risk", {}))
        for optional_int_key in (
            "max_positions_per_symbol",
            "max_open_positions_total",
            "max_pending_orders_per_symbol",
        ):
            if str(risk_config.get(optional_int_key, "")).strip() == "":
                risk_config[optional_int_key] = None
        for optional_float_key in ("max_volume_per_order", "max_volume_per_symbol"):
            if str(risk_config.get(optional_float_key, "")).strip() == "":
                risk_config[optional_float_key] = None
        self._set_provenance(
            "api",
            "host",
            self._option_source(
                config_name="market.ini",
                section="api",
                key="host",
                fallback=self._option_source(
                    config_name="app.ini",
                    section="system",
                    key="api_host",
                ),
            ),
        )
        self._set_provenance(
            "api",
            "port",
            self._option_source(
                config_name="market.ini",
                section="api",
                key="port",
                fallback=self._option_source(
                    config_name="app.ini",
                    section="system",
                    key="api_port",
                ),
            ),
        )
        for field in APIConfig.model_fields:
            if field in {"host", "port"}:
                continue
            if field in {"auth_enabled", "api_key_header", "api_key"}:
                source = self._option_source(config_name="market.ini", section="security", key=field)
            elif field in {"access_log_enabled", "log_format"}:
                source = self._option_source(config_name="market.ini", section="logging", key=field)
            else:
                source = self._option_source(config_name="market.ini", section="api", key=field)
            self._set_provenance("api", field, source)
        for field in IngestConfig.model_fields:
            if field in configs["ingest"].get("ingest", {}):
                source = "ingest.ini[ingest]." + field
            elif field in configs["ingest"].get("performance", {}):
                source = "ingest.ini[performance]." + field
            elif field in configs["ingest"].get("health", {}):
                source = "ingest.ini[health]." + field
            else:
                source = "default"
            self._set_provenance("ingest", field, source)
        for field in EconomicConfig.model_fields:
            if field == "local_timezone":
                source = self._option_source(
                    config_name="economic.ini",
                    section="economic",
                    key="local_timezone",
                    fallback=self._option_source(
                        config_name="app.ini",
                        section="system",
                        key="timezone",
                    ),
                )
            elif field == "tradingeconomics_enabled":
                source = self._option_source(config_name="economic.ini", section="tradingeconomics", key="enabled")
            elif field == "fred_enabled":
                source = self._option_source(config_name="economic.ini", section="fred", key="enabled")
            elif field == "tradingeconomics_api_key":
                source = self._option_source(config_name="economic.ini", section="tradingeconomics", key="api_key")
            elif field == "fred_api_key":
                source = self._option_source(config_name="economic.ini", section="fred", key="api_key")
            else:
                source = self._option_source(config_name="economic.ini", section="economic", key=field)
            self._set_provenance("economic", field, source)
        for field in RiskConfig.model_fields:
            self._set_provenance(
                "risk",
                field,
                self._option_source(config_name="risk.ini", section="risk", key=field),
            )
        self._config_cache = {
            **shared_config,
            "api": APIConfig(**api_config).model_dump(),
            "ingest": IngestConfig(**ingest_config).model_dump(),
            "economic": EconomicConfig(**economic_config).model_dump(),
            "risk": RiskConfig(**risk_config).model_dump(),
            "trading_ops": TradingOpsConfig(**dict(configs["main"].get("trading_ops", {}))).model_dump(),
            "raw": {
                "mt5": configs["mt5"],
                "db": configs["db"],
                "storage": configs["storage"],
                "cache": configs["cache"],
                "indicators": configs["indicators"],
                "economic": configs["economic"],
                "risk": configs["risk"],
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

    def get_economic_config(self) -> EconomicConfig:
        return EconomicConfig(**self.load_all()["economic"])

    def get_risk_config(self) -> RiskConfig:
        return RiskConfig(**self.load_all()["risk"])

    def get_trading_ops_config(self) -> TradingOpsConfig:
        return TradingOpsConfig(**self.load_all()["trading_ops"])

    def get_raw_config(self, module: str) -> Dict[str, Any]:
        return self.load_all()["raw"].get(module, {})

    def reload(self):
        self._config_cache.clear()
        self._validated = False
        self.load_all()

    def get_effective_config_snapshot(self) -> Dict[str, Any]:
        config = self.load_all()
        api_snapshot = dict(config["api"])
        if api_snapshot.get("api_key"):
            api_snapshot["api_key"] = "***"
        economic_snapshot = dict(config["economic"])
        if economic_snapshot.get("tradingeconomics_api_key"):
            economic_snapshot["tradingeconomics_api_key"] = "***"
        if economic_snapshot.get("fred_api_key"):
            economic_snapshot["fred_api_key"] = "***"
        return {
            "trading": dict(config["trading"]),
            "intervals": dict(config["intervals"]),
            "limits": dict(config["limits"]),
            "system": dict(config["system"]),
            "api": api_snapshot,
            "ingest": dict(config["ingest"]),
            "economic": economic_snapshot,
            "risk": dict(config["risk"]),
            "trading_ops": dict(config["trading_ops"]),
            "storage": dict(config["raw"].get("storage", {})),
            "provenance": self.get_config_provenance_snapshot(),
        }

    def get_config_provenance_snapshot(self) -> Dict[str, Dict[str, str]]:
        self.load_all()
        return {section: dict(entries) for section, entries in self._provenance_cache.items()}


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


@lru_cache
def get_economic_config() -> EconomicConfig:
    return _config_manager.get_economic_config()


@lru_cache
def get_risk_config() -> RiskConfig:
    return _config_manager.get_risk_config()


@lru_cache
def get_trading_ops_config() -> TradingOpsConfig:
    return _config_manager.get_trading_ops_config()


def reload_configs():
    get_trading_config.cache_clear()
    get_interval_config.cache_clear()
    get_limit_config.cache_clear()
    get_api_config.cache_clear()
    get_ingest_config.cache_clear()
    get_system_config.cache_clear()
    get_economic_config.cache_clear()
    get_risk_config.cache_clear()
    get_trading_ops_config.cache_clear()
    # Compatibility loaders keep their own lru_cache; clear them so reload has
    # consistent semantics across both primary and legacy call paths.
    from src.config.compat import (
        load_db_settings,
        load_indicator_settings,
        load_ingest_settings,
        load_market_settings,
        load_mt5_settings,
        load_storage_settings,
    )

    load_mt5_settings.cache_clear()
    load_db_settings.cache_clear()
    load_ingest_settings.cache_clear()
    load_storage_settings.cache_clear()
    load_market_settings.cache_clear()
    load_indicator_settings.cache_clear()
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


def get_effective_config_snapshot() -> Dict[str, Any]:
    return _config_manager.get_effective_config_snapshot()


def get_config_provenance_snapshot() -> Dict[str, Dict[str, str]]:
    return _config_manager.get_config_provenance_snapshot()
