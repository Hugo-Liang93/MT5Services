from __future__ import annotations

import configparser

import src.config.centralized as centralized
import src.config.mt5 as mt5_config
import src.config.runtime as runtime_views


def _parser_with_section(section: str, values: dict[str, object]) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.add_section(section)
    for key, value in values.items():
        parser.set(section, key, str(value))
    return parser


def test_runtime_loaders_use_merged_ini(monkeypatch):
    parsers = {
        "mt5.ini": _parser_with_section(
            "mt5",
            {
                "login": 123456,
                "password": "secret",
                "server": "demo",
                "path": "C:/MT5/terminal64.exe",
            },
        ),
        "cache.ini": _parser_with_section(
            "cache",
            {
                "tick_cache_size": 777,
                "ohlc_cache_limit": 222,
                "intrabar_max_points": 333,
                "ohlc_event_queue_size": 444,
            },
        ),
    }

    def fake_load_config_with_base(config_name: str, base_config: str = "app.ini", base_dir=None):
        return config_name, parsers.get(config_name)

    monkeypatch.setattr(mt5_config, "load_config_with_base", fake_load_config_with_base)
    monkeypatch.setattr(runtime_views, "load_config_with_base", fake_load_config_with_base)

    mt5_config.load_mt5_settings.cache_clear()
    runtime_views.get_runtime_market_settings.cache_clear()
    try:
        mt5_settings = mt5_config.load_mt5_settings()
        market_settings = runtime_views.get_runtime_market_settings()

        assert mt5_settings.mt5_login == 123456
        assert mt5_settings.mt5_server == "demo"
        assert market_settings.tick_cache_size == 777
        assert market_settings.ohlc_event_queue_size == 444
    finally:
        mt5_config.load_mt5_settings.cache_clear()
        runtime_views.get_runtime_market_settings.cache_clear()


def test_reload_configs_clears_runtime_loader_caches(monkeypatch):
    state = {"login": 1001, "tick_cache_size": 111}

    def fake_load_config_with_base(config_name: str, base_config: str = "app.ini", base_dir=None):
        if config_name == "mt5.ini":
            return config_name, _parser_with_section("mt5", {"login": state["login"]})
        if config_name == "cache.ini":
            return config_name, _parser_with_section("cache", {"tick_cache_size": state["tick_cache_size"]})
        return config_name, None

    monkeypatch.setattr(mt5_config, "load_config_with_base", fake_load_config_with_base)
    monkeypatch.setattr(runtime_views, "load_config_with_base", fake_load_config_with_base)

    mt5_config.load_mt5_settings.cache_clear()
    runtime_views.get_runtime_market_settings.cache_clear()
    try:
        assert mt5_config.load_mt5_settings().mt5_login == 1001
        assert runtime_views.get_runtime_market_settings().tick_cache_size == 111

        state["login"] = 2002
        state["tick_cache_size"] = 222

        centralized.reload_configs()

        assert mt5_config.load_mt5_settings().mt5_login == 2002
        assert runtime_views.get_runtime_market_settings().tick_cache_size == 222
    finally:
        mt5_config.load_mt5_settings.cache_clear()
        runtime_views.get_runtime_market_settings.cache_clear()


def test_mt5_loader_strips_wrapping_quotes_from_ini_values(monkeypatch):
    parser = _parser_with_section(
        "mt5",
        {
            "login": 123456,
            "password": '"secret"',
            "server": '"demo-server"',
            "path": '"C:/Program Files/MT5/terminal64.exe"',
        },
    )

    monkeypatch.setattr(
        mt5_config,
        "load_config_with_base",
        lambda config_name, base_config="app.ini", base_dir=None: (config_name, parser)
        if config_name == "mt5.ini"
        else (config_name, None),
    )

    mt5_config.load_mt5_settings.cache_clear()
    try:
        settings = mt5_config.load_mt5_settings()
        assert settings.mt5_password == "secret"
        assert settings.mt5_server == "demo-server"
        assert settings.mt5_path == "C:/Program Files/MT5/terminal64.exe"
    finally:
        mt5_config.load_mt5_settings.cache_clear()


def test_provenance_reports_local_ini_sources(monkeypatch):
    config_map = {
        "app.ini": {
            "trading": {
                "symbols": "XAUUSD",
                "timeframes": "M1,H1",
                "default_symbol": "XAUUSD",
            },
            "intervals": {},
            "limits": {},
            "system": {
                "api_host": "0.0.0.0",
                "api_port": 8899,
                "timezone": "Asia/Shanghai",
            },
        },
        "market.ini": {
            "api": {},
            "security": {"api_key": "local-secret"},
        },
        "ingest.ini": {},
        "economic.ini": {
            "fred": {"enabled": True},
            "tradingeconomics": {"enabled": True},
        },
        "mt5.ini": {},
        "db.ini": {},
        "storage.ini": {},
        "cache.ini": {},
    }
    source_map = {
        ("app.ini", "trading", "symbols"): "app.ini[trading].symbols",
        ("app.ini", "trading", "timeframes"): "app.ini[trading].timeframes",
        ("app.ini", "trading", "default_symbol"): "app.ini[trading].default_symbol",
        ("app.ini", "system", "api_host"): "app.local.ini[system].api_host",
        ("app.ini", "system", "api_port"): "app.local.ini[system].api_port",
        ("app.ini", "system", "timezone"): "app.local.ini[system].timezone",
        ("market.ini", "security", "api_key"): "market.local.ini[security].api_key",
        ("economic.ini", "jin10", "enabled"): "economic.local.ini[jin10].enabled",
        ("economic.ini", "jin10", "token"): "economic.local.ini[jin10].token",
    }

    monkeypatch.setattr(centralized, "get_merged_config", lambda name: config_map.get(name, {}))
    monkeypatch.setattr(
        centralized,
        "get_merged_option_source",
        lambda config_name, section, key, base_config="app.ini", base_dir=None: source_map.get(
            (config_name, section, key)
        ),
    )

    manager = centralized.CentralizedConfig()
    provenance = manager.get_config_provenance_snapshot()

    assert provenance["api"]["host"] == "app.local.ini[system].api_host"
    assert provenance["api"]["port"] == "app.local.ini[system].api_port"
    assert provenance["api"]["api_key"] == "market.local.ini[security].api_key"
    assert provenance["economic"]["local_timezone"] == "app.local.ini[system].timezone"


def test_effective_config_snapshot_masks_jin10_token(monkeypatch):
    config_map = {
        "app.ini": {
            "trading": {
                "symbols": "XAUUSD",
                "timeframes": "M1",
                "default_symbol": "XAUUSD",
            },
            "intervals": {},
            "limits": {},
            "system": {},
        },
        "market.ini": {"security": {"api_key": "api-secret"}},
        "ingest.ini": {},
        "economic.ini": {
            "jin10": {"enabled": True, "token": "jin10-secret"},
            "tradingeconomics": {"enabled": False},
            "fred": {"enabled": False},
        },
        "mt5.ini": {},
        "db.ini": {},
        "storage.ini": {},
        "cache.ini": {},
    }

    monkeypatch.setattr(centralized, "get_merged_config", lambda name: config_map.get(name, {}))
    source_map = {
        ("economic.ini", "jin10", "enabled"): "economic.local.ini[jin10].enabled",
        ("economic.ini", "jin10", "token"): "economic.local.ini[jin10].token",
        ("market.ini", "security", "api_key"): "market.local.ini[security].api_key",
    }
    monkeypatch.setattr(
        centralized,
        "get_merged_option_source",
        lambda config_name, section, key, base_config="app.ini", base_dir=None: source_map.get(
            (config_name, section, key)
        ),
    )

    manager = centralized.CentralizedConfig()
    snapshot = manager.get_effective_config_snapshot()
    provenance = manager.get_config_provenance_snapshot()

    assert snapshot["api"]["api_key"] == "***"
    assert snapshot["economic"]["jin10_token"] == "***"
    assert provenance["economic"]["jin10_enabled"] == "economic.local.ini[jin10].enabled"
    assert provenance["economic"]["jin10_token"] == "economic.local.ini[jin10].token"
