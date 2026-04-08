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
from src.config.indicator_config import ConfigLoader
from src.config.utils import ConfigValidator


def test_centralized_config():
    valid, message = validate_config_consistency()
    assert valid, message

    trading = get_trading_config()
    assert trading.symbols == ["XAUUSD"]
    assert trading.default_symbol == "XAUUSD"
    assert "M5" in trading.timeframes
    assert "H1" in trading.timeframes
    assert "M30" in trading.timeframes
    assert "H4" in trading.timeframes

    intervals = get_interval_config()
    assert intervals.poll_interval == 0.5
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
    assert system.timezone in ("UTC", "Asia/Shanghai")
    assert "ingest" in system.modules_enabled
    assert "api" in system.modules_enabled

    assert get_shared_symbols() == ["XAUUSD"]
    assert get_shared_timeframes() == ["M5", "M15", "M30", "H1", "H4", "D1"]
    assert get_shared_default_symbol() == "XAUUSD"


def test_runtime_views_align_with_centralized_config():
    ingest_settings = get_runtime_ingest_settings()
    market_settings = get_runtime_market_settings()

    assert ingest_settings.ingest_symbols == ["XAUUSD"]
    assert ingest_settings.ingest_poll_interval == 0.5
    assert "M5" in ingest_settings.ingest_ohlc_timeframes or "H1" in ingest_settings.ingest_ohlc_timeframes
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
            "poll_interval": "1.0",
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


def test_indicator_config_reflects_phase1_intraday_changes():
    config = ConfigLoader.load("config/indicators.json")
    indicators = {indicator.name: indicator for indicator in config.indicators}

    assert {"rsi5", "macd_fast", "ema21", "ema55"} <= set(indicators)
    # rsi5/macd_fast/ema21/ema55 无策略依赖，无 display 标记（rsi14/macd/ema9+ema50 已覆盖）
    assert indicators["rsi5"].display is False
    assert indicators["macd_fast"].display is False
    assert indicators["ema21"].display is False
    assert indicators["ema55"].display is False
    assert indicators["mfi14"].display is False
    assert indicators["obv30"].display is False
    assert indicators["wma20"].display is False
    assert indicators["rsi14"].delta_bars == [3, 5]
    assert indicators["macd"].delta_bars == []
    assert indicators["adx14"].delta_bars == [3, 5]
    assert indicators["cci20"].delta_bars == [3, 5]
    assert indicators["stoch_rsi14"].delta_bars == [3, 5]
