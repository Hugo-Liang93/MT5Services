"""
Centralized configuration manager.

This module remains the runtime aggregation entrypoint while config models and
signal-specific loading live in smaller submodules.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, List

from src.config.models import (
    APIConfig,
    EconomicConfig,
    IngestConfig,
    IntervalConfig,
    LimitConfig,
    RiskConfig,
    SignalConfig,
    SystemConfig,
    TradingConfig,
    TradingOpsConfig,
)
from src.config.signal import get_signal_config
from src.config.utils import (
    ConfigValidator,
    get_merged_config,
    get_merged_option_source,
)


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

    def _load_and_validate(self) -> None:
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
            self._set_provenance(
                "trading",
                field,
                self._option_source(config_name="app.ini", section="trading", key=field),
            )
        for field in shared["intervals"]:
            self._set_provenance(
                "intervals",
                field,
                self._option_source(config_name="app.ini", section="intervals", key=field),
            )
        for field in shared["limits"]:
            self._set_provenance(
                "limits",
                field,
                self._option_source(config_name="app.ini", section="limits", key=field),
            )
        for field in shared["system"]:
            self._set_provenance(
                "system",
                field,
                self._option_source(config_name="app.ini", section="system", key=field),
            )
        return shared

    def _validate_configs(
        self,
        shared_config: Dict[str, Any],
        configs: Dict[str, Dict[str, Any]],
    ) -> None:
        ConfigValidator.validate_trading_config(shared_config)
        ConfigValidator.validate_interval_config(shared_config)
        self._check_config_inheritance(shared_config, configs)

    def _check_config_inheritance(
        self,
        shared_config: Dict[str, Any],
        configs: Dict[str, Dict[str, Any]],
    ) -> None:
        trading_symbols = shared_config["trading"]["symbols"]
        api_config = configs["api"]
        if "default_symbol" in api_config.get("api", {}):
            api_default = api_config["api"]["default_symbol"]
            if api_default not in trading_symbols:
                raise ValueError(
                    f"API default symbol '{api_default}' not in trading symbols: {trading_symbols}"
                )

    def _build_final_config(
        self,
        shared_config: Dict[str, Any],
        configs: Dict[str, Dict[str, Any]],
    ) -> None:
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
            configs["ingest"].get("error_recovery", {}),
        )
        # 从各 provider section 提取配置（过滤 None 避免覆盖 Pydantic 默认值）
        _provider_fields: Dict[str, Any] = {}
        for prefix, section_name in (
            ("tradingeconomics", "tradingeconomics"),
            ("fred", "fred"),
            ("fmp", "fmp"),
            ("jin10", "jin10"),
            ("alphavantage", "alphavantage"),
        ):
            section_data = configs["economic"].get(section_name, {})
            for key in ("enabled", "api_key"):
                val = section_data.get(key)
                if val is not None:
                    _provider_fields[f"{prefix}_{key}"] = val
        # Jin10 token（非 api_key 字段名，单独处理）
        _jin10_section = configs["economic"].get("jin10", {})
        _jin10_token = _jin10_section.get("token")
        if _jin10_token is not None:
            _provider_fields["jin10_token"] = _jin10_token
        # market_impact section 键名需要加前缀
        _market_impact_raw = configs["economic"].get("market_impact", {})
        _market_impact_fields = {
            f"market_impact_{k}": v for k, v in _market_impact_raw.items() if v is not None
        }
        economic_config = _merge_sections(
            {"local_timezone": shared_config["system"].get("timezone", "UTC")},
            configs["economic"].get("economic", {}),
            _provider_fields,
            _market_impact_fields,
        )
        # Alpha Vantage tracked_indicators（逗号分隔列表）
        av_section = configs["economic"].get("alphavantage", {})
        if "tracked_indicators" in av_section:
            economic_config["alphavantage_tracked_indicators"] = _split_csv(
                av_section["tracked_indicators"]
            )
        if "default_countries" in economic_config:
            economic_config["default_countries"] = _split_csv(
                economic_config["default_countries"]
            )
        for list_key in (
            "fred_release_whitelist_ids",
            "fred_release_whitelist_keywords",
            "fred_release_blacklist_keywords",
            "calendar_sync_sources",
            "near_term_sync_sources",
            "release_watch_sources",
            "curated_sources",
            "curated_countries",
            "curated_currencies",
            "curated_statuses",
            "market_impact_symbols",
            "market_impact_timeframes",
        ):
            if list_key in economic_config:
                economic_config[list_key] = _split_csv(economic_config[list_key])
        # market_impact int 列表
        for int_list_key in ("market_impact_pre_windows", "market_impact_post_windows"):
            if int_list_key in economic_config:
                raw = economic_config[int_list_key]
                if isinstance(raw, str):
                    economic_config[int_list_key] = [
                        int(v.strip()) for v in raw.split(",") if v.strip()
                    ]
        for optional_int_key in ("curated_importance_min", "trade_guard_importance_min"):
            if str(economic_config.get(optional_int_key, "")).strip() == "":
                economic_config[optional_int_key] = None
        if (
            "near_term_refresh_interval_seconds" not in economic_config
            and "refresh_interval_seconds" in economic_config
        ):
            economic_config["near_term_refresh_interval_seconds"] = economic_config[
                "refresh_interval_seconds"
            ]
        for api_key_field in (
            "tradingeconomics_api_key",
            "fred_api_key",
            "fmp_api_key",
            "alphavantage_api_key",
            "jin10_token",
        ):
            economic_config[api_key_field] = _normalize_optional_secret(
                economic_config.get(api_key_field)
            )

        risk_config = dict(configs["risk"].get("risk", {}))
        for optional_int_key in (
            "max_positions_per_symbol",
            "max_open_positions_total",
            "max_pending_orders_per_symbol",
            "max_trades_per_day",
            "max_trades_per_hour",
        ):
            if str(risk_config.get(optional_int_key, "")).strip() == "":
                risk_config[optional_int_key] = None
        for optional_float_key in (
            "max_volume_per_order",
            "max_volume_per_symbol",
            "max_net_lots_per_symbol",
            "daily_loss_limit_pct",
        ):
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
                source = self._option_source(
                    config_name="market.ini",
                    section="security",
                    key=field,
                )
            elif field in {"access_log_enabled", "log_format"}:
                source = self._option_source(
                    config_name="market.ini",
                    section="logging",
                    key=field,
                )
            else:
                source = self._option_source(
                    config_name="market.ini",
                    section="api",
                    key=field,
                )
            self._set_provenance("api", field, source)

        for field in IngestConfig.model_fields:
            if field in configs["ingest"].get("ingest", {}):
                source = "ingest.ini[ingest]." + field
            elif field in configs["ingest"].get("performance", {}):
                source = "ingest.ini[performance]." + field
            elif field in configs["ingest"].get("health", {}):
                source = "ingest.ini[health]." + field
            elif field in configs["ingest"].get("error_recovery", {}):
                source = "ingest.ini[error_recovery]." + field
            else:
                source = "default"
            self._set_provenance("ingest", field, source)

        _PROVIDER_SECTION_MAP = {
            "tradingeconomics_enabled": ("tradingeconomics", "enabled"),
            "tradingeconomics_api_key": ("tradingeconomics", "api_key"),
            "fred_enabled": ("fred", "enabled"),
            "fred_api_key": ("fred", "api_key"),
            "fmp_enabled": ("fmp", "enabled"),
            "fmp_api_key": ("fmp", "api_key"),
            "alphavantage_enabled": ("alphavantage", "enabled"),
            "alphavantage_api_key": ("alphavantage", "api_key"),
            "alphavantage_tracked_indicators": ("alphavantage", "tracked_indicators"),
        }
        _MARKET_IMPACT_FIELDS = {f for f in EconomicConfig.model_fields if f.startswith("market_impact_")}
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
            elif field in _PROVIDER_SECTION_MAP:
                section, key = _PROVIDER_SECTION_MAP[field]
                source = self._option_source(
                    config_name="economic.ini",
                    section=section,
                    key=key,
                )
            elif field in _MARKET_IMPACT_FIELDS:
                ini_key = field.removeprefix("market_impact_")
                source = self._option_source(
                    config_name="economic.ini",
                    section="market_impact",
                    key=ini_key,
                )
            else:
                source = self._option_source(
                    config_name="economic.ini",
                    section="economic",
                    key=field,
                )
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
            "trading_ops": TradingOpsConfig(
                **dict(configs["main"].get("trading_ops", {}))
            ).model_dump(),
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

    def reload(self) -> None:
        self._config_cache.clear()
        self._validated = False
        self.load_all()

    def get_effective_config_snapshot(self) -> Dict[str, Any]:
        config = self.load_all()
        api_snapshot = dict(config["api"])
        if api_snapshot.get("api_key"):
            api_snapshot["api_key"] = "***"
        economic_snapshot = dict(config["economic"])
        for key in ("tradingeconomics_api_key", "fred_api_key", "fmp_api_key", "alphavantage_api_key"):
            if economic_snapshot.get(key):
                economic_snapshot[key] = "***"
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
        return {
            section: dict(entries)
            for section, entries in self._provenance_cache.items()
        }


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


def reload_configs() -> None:
    get_trading_config.cache_clear()
    get_interval_config.cache_clear()
    get_limit_config.cache_clear()
    get_api_config.cache_clear()
    get_ingest_config.cache_clear()
    get_system_config.cache_clear()
    get_economic_config.cache_clear()
    get_risk_config.cache_clear()
    get_trading_ops_config.cache_clear()
    get_signal_config.cache_clear()
    from src.config.runtime import (
        get_runtime_ingest_settings,
        get_runtime_market_settings,
    )
    from src.config.database import load_db_settings
    from src.config.indicator_runtime import load_indicator_settings
    from src.config.mt5 import load_mt5_settings
    from src.config.storage import load_storage_settings

    get_runtime_ingest_settings.cache_clear()
    get_runtime_market_settings.cache_clear()
    load_mt5_settings.cache_clear()
    load_db_settings.cache_clear()
    load_storage_settings.cache_clear()
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
    except Exception as exc:
        return False, f"config validation failed: {exc}"


def get_effective_config_snapshot() -> Dict[str, Any]:
    return _config_manager.get_effective_config_snapshot()


def get_config_provenance_snapshot() -> Dict[str, Dict[str, str]]:
    return _config_manager.get_config_provenance_snapshot()
