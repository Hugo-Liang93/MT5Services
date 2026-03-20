from __future__ import annotations

from src.config import (
    get_api_config,
    get_ingest_config,
    get_interval_config,
    get_limit_config,
    get_runtime_ingest_settings,
    get_runtime_market_settings,
    get_shared_default_symbol,
    get_shared_symbols,
    get_shared_timeframes,
    get_system_config,
    get_trading_config,
    validate_config_consistency,
)
from src.config.file_manager import FileConfigManager
from src.config.utils import ConfigValidator


def test_centralized_config():
    valid, message = validate_config_consistency()
    assert valid, message

    trading = get_trading_config()
    assert trading.symbols == ["XAUUSD"]
    assert trading.default_symbol == "XAUUSD"
    assert "M1" in trading.timeframes
    assert "H1" in trading.timeframes

    intervals = get_interval_config()
    assert intervals.tick_interval == 0.5
    assert intervals.ohlc_interval == 30.0

    limits = get_limit_config()
    assert limits.tick_limit == 200
    assert limits.ohlc_limit == 200

    api = get_api_config()
    assert api.host == "0.0.0.0"
    assert api.port == 8808

    ingest = get_ingest_config()
    assert ingest.tick_initial_lookback_seconds == 20
    assert ingest.ohlc_backfill_limit == 500

    system = get_system_config()
    assert system.timezone == "UTC"
    assert "ingest" in system.modules_enabled
    assert "api" in system.modules_enabled

    assert get_shared_symbols() == ["XAUUSD"]
    assert get_shared_timeframes() == ["M1", "H1"]
    assert get_shared_default_symbol() == "XAUUSD"


def test_runtime_views_align_with_centralized_config():
    ingest_settings = get_runtime_ingest_settings()
    market_settings = get_runtime_market_settings()

    assert ingest_settings.ingest_symbols == ["XAUUSD"]
    assert ingest_settings.ingest_tick_interval == 0.5
    assert "M1" in ingest_settings.ingest_ohlc_timeframes
    assert hasattr(ingest_settings, "intrabar_enabled")

    assert market_settings.default_symbol == "XAUUSD"
    assert market_settings.tick_limit == 200
    assert market_settings.stream_interval_seconds == 1.0

    assert ingest_settings.ingest_symbols[0] == market_settings.default_symbol


def test_config_validation_helpers():
    valid_config = {
        "trading": {
            "symbols": "XAUUSD,EURUSD",
            "default_symbol": "XAUUSD",
            "timeframes": "M1,H1",
        },
        "intervals": {
            "tick_interval": "1.0",
            "ohlc_interval": "60.0",
        },
    }

    ConfigValidator.validate_trading_config(valid_config)
    ConfigValidator.validate_interval_config(valid_config)

    invalid_config = {
        "trading": {
            "symbols": "",
            "default_symbol": "INVALID",
            "timeframes": "INVALID",
        }
    }

    try:
        ConfigValidator.validate_trading_config(invalid_config)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid trading config should fail validation")


def test_file_manager_reads_indicators_json():
    manager = FileConfigManager("config")
    try:
        poll_interval = manager.get("indicators.json", "pipeline", "poll_interval")
        reload_interval = manager.get("indicators.json", "root", "reload_interval")

        assert poll_interval == 5
        assert reload_interval == 60
    finally:
        manager.stop()
