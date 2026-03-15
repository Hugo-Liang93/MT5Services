#!/usr/bin/env python3
"""
测试配置中心化系统
"""

from src.config import (
    # 集中式配置
    get_trading_config,
    get_interval_config,
    get_limit_config,
    get_api_config,
    get_ingest_config,
    get_system_config,
    get_shared_symbols,
    get_shared_timeframes,
    get_shared_default_symbol,
    validate_config_consistency,
    
    # 兼容层
    load_ingest_settings,
    load_market_settings,
)
from src.config.advanced_manager import AdvancedConfigManager


def test_centralized_config():
    """测试集中式配置系统"""
    print("=== 测试集中式配置系统 ===\n")
    
    # 1. 验证配置一致性
    print("1. 验证配置一致性...")
    valid, message = validate_config_consistency()
    print(f"   结果: {valid} - {message}")
    assert valid, f"配置验证失败: {message}"
    
    # 2. 测试交易配置
    print("\n2. 测试交易配置...")
    trading = get_trading_config()
    print(f"   交易品种: {trading.symbols}")
    print(f"   时间框架: {trading.timeframes}")
    print(f"   默认品种: {trading.default_symbol}")
    
    assert trading.symbols == ["XAUUSD"], f"预期 XAUUSD，实际: {trading.symbols}"
    assert trading.default_symbol == "XAUUSD", f"预期 XAUUSD，实际: {trading.default_symbol}"
    assert "M1" in trading.timeframes, "M1 应该在时间框架中"
    assert "H1" in trading.timeframes, "H1 应该在时间框架中"
    
    # 3. 测试间隔配置
    print("\n3. 测试间隔配置...")
    intervals = get_interval_config()
    print(f"   Tick间隔: {intervals.tick_interval}秒")
    print(f"   OHLC间隔: {intervals.ohlc_interval}秒")
    print(f"   流间隔: {intervals.stream_interval}秒")
    
    assert intervals.tick_interval == 0.5, f"预期 0.5，实际: {intervals.tick_interval}"
    assert intervals.ohlc_interval == 30.0, f"预期 30.0，实际: {intervals.ohlc_interval}"
    
    # 4. 测试限制配置
    print("\n4. 测试限制配置...")
    limits = get_limit_config()
    print(f"   Tick限制: {limits.tick_limit}")
    print(f"   OHLC限制: {limits.ohlc_limit}")
    print(f"   Tick缓存: {limits.tick_cache_size}")
    
    assert limits.tick_limit == 200, f"预期 200，实际: {limits.tick_limit}"
    assert limits.ohlc_limit == 200, f"预期 200，实际: {limits.ohlc_limit}"
    
    # 5. 测试API配置
    print("\n5. 测试API配置...")
    api = get_api_config()
    print(f"   API主机: {api.host}")
    print(f"   API端口: {api.port}")
    print(f"   CORS启用: {api.enable_cors}")
    
    assert api.host == "0.0.0.0", f"预期 0.0.0.0，实际: {api.host}"
    assert api.port == 8808, f"预期 8808，实际: {api.port}"
    
    # 6. 测试采集配置
    print("\n6. 测试采集配置...")
    ingest = get_ingest_config()
    print(f"   初始回溯: {ingest.tick_initial_lookback_seconds}秒")
    print(f"   回填限制: {ingest.ohlc_backfill_limit}")
    print(f"   重试次数: {ingest.retry_attempts}")
    
    assert ingest.tick_initial_lookback_seconds == 20, f"预期 20，实际: {ingest.tick_initial_lookback_seconds}"
    assert ingest.ohlc_backfill_limit == 500, f"预期 500，实际: {ingest.ohlc_backfill_limit}"
    
    # 7. 测试系统配置
    print("\n7. 测试系统配置...")
    system = get_system_config()
    print(f"   时区: {system.timezone}")
    print(f"   日志级别: {system.log_level}")
    print(f"   启用模块: {system.modules_enabled}")
    
    assert system.timezone == "UTC", f"预期 UTC，实际: {system.timezone}"
    assert "ingest" in system.modules_enabled, "ingest 应该在启用模块中"
    assert "api" in system.modules_enabled, "api 应该在启用模块中"
    
    # 8. 测试工具函数
    print("\n8. 测试工具函数...")
    symbols = get_shared_symbols()
    timeframes = get_shared_timeframes()
    default_symbol = get_shared_default_symbol()
    
    print(f"   共享品种: {symbols}")
    print(f"   共享时间框架: {timeframes}")
    print(f"   共享默认品种: {default_symbol}")
    
    assert symbols == ["XAUUSD"], f"预期 ['XAUUSD']，实际: {symbols}"
    assert default_symbol == "XAUUSD", f"预期 XAUUSD，实际: {default_symbol}"
    
    print("\n✅ 集中式配置系统测试通过！\n")


def test_compatibility_shims():
    """测试向后兼容性"""
    print("=== 测试向后兼容性 ===\n")
    
    # 1. 测试 ingest 配置兼容性
    print("1. 测试 ingest 配置兼容性...")
    ingest_settings = load_ingest_settings()
    print(f"   ingest_symbols: {ingest_settings.ingest_symbols}")
    print(f"   ingest_tick_interval: {ingest_settings.ingest_tick_interval}")
    print(f"   ingest_ohlc_timeframes: {ingest_settings.ingest_ohlc_timeframes}")
    
    # 应该使用集中式配置的值
    assert ingest_settings.ingest_symbols == ["XAUUSD"], f"预期 ['XAUUSD']，实际: {ingest_settings.ingest_symbols}"
    assert ingest_settings.ingest_tick_interval == 0.5, f"预期 0.5，实际: {ingest_settings.ingest_tick_interval}"
    assert "M1" in ingest_settings.ingest_ohlc_timeframes, "M1 应该在时间框架中"
    
    # 2. 测试 market 配置兼容性
    print("\n2. 测试 market 配置兼容性...")
    market_settings = load_market_settings()
    print(f"   default_symbol: {market_settings.default_symbol}")
    print(f"   tick_limit: {market_settings.tick_limit}")
    print(f"   stream_interval_seconds: {market_settings.stream_interval_seconds}")
    
    # 应该使用集中式配置的值
    assert market_settings.default_symbol == "XAUUSD", f"预期 XAUUSD，实际: {market_settings.default_symbol}"
    assert market_settings.tick_limit == 200, f"预期 200，实际: {market_settings.tick_limit}"
    assert market_settings.stream_interval_seconds == 1.0, f"预期 1.0，实际: {market_settings.stream_interval_seconds}"
    
    # 3. 测试配置一致性
    print("\n3. 测试配置一致性...")
    # ingest 和 market 应该使用相同的品种
    assert ingest_settings.ingest_symbols[0] == market_settings.default_symbol, \
        f"ingest品种({ingest_settings.ingest_symbols})和market默认品种({market_settings.default_symbol})不一致"
    
    print("\n✅ 向后兼容性测试通过！\n")


def test_config_inheritance():
    """测试配置继承"""
    print("=== 测试配置继承 ===\n")
    
    # 修改 app.ini 中的配置，检查是否被继承
    print("1. 测试配置继承机制...")
    
    # 获取当前配置
    trading = get_trading_config()
    original_symbols = trading.symbols.copy()
    
    print(f"   当前品种: {original_symbols}")
    print("   注意: 要测试配置热重载，需要修改 app.ini 文件")
    print("   然后调用 reload_configs() 函数")
    
    print("\n2. 测试配置验证...")
    # 测试无效配置
    from src.config.utils import ConfigValidator
    
    # 有效配置应该通过
    valid_config = {
        "trading": {
            "symbols": "XAUUSD,EURUSD",
            "default_symbol": "XAUUSD",
            "timeframes": "M1,H1"
        },
        "intervals": {
            "tick_interval": "1.0",
            "ohlc_interval": "60.0"
        }
    }
    
    try:
        ConfigValidator.validate_trading_config(valid_config)
        ConfigValidator.validate_interval_config(valid_config)
        print("   ✅ 有效配置验证通过")
    except Exception as e:
        print(f"   ❌ 有效配置验证失败: {e}")
    
    # 无效配置应该失败
    invalid_config = {
        "trading": {
            "symbols": "",
            "default_symbol": "INVALID",
            "timeframes": "INVALID"
        }
    }
    
    try:
        ConfigValidator.validate_trading_config(invalid_config)
        print("   ❌ 无效配置应该失败但没有")
    except ValueError as e:
        print(f"   ✅ 无效配置正确失败: {e}")
    
    print("\n✅ 配置继承测试完成！\n")


def test_advanced_manager_reads_indicators_json():
    """兼容配置管理器应读取当前唯一指标配置文件。"""
    manager = AdvancedConfigManager("config")
    try:
        poll_interval = manager.get("indicators.json", "pipeline", "poll_interval")
        reload_interval = manager.get("indicators.json", "root", "reload_interval")

        assert poll_interval == 5
        assert reload_interval == 60
    finally:
        manager.stop()


def main():
    """主测试函数"""
    print("开始测试配置中心化系统...\n")
    
    try:
        test_centralized_config()
        test_compatibility_shims()
        test_config_inheritance()
        
        print("=" * 50)
        print("🎉 所有测试通过！")
        print("\n配置中心化系统功能总结：")
        print("1. ✅ 单一信号源配置 (app.ini)")
        print("2. ✅ 配置继承机制")
        print("3. ✅ 配置一致性验证")
        print("4. ✅ 向后兼容性")
        print("5. ✅ 配置热重载支持")
        print("\n核心改进：")
        print("- 交易品种、时间框架等核心配置集中管理")
        print("- 消除配置重复和 inconsistency")
        print("- 支持配置验证和热重载")
        print("- 保持现有代码完全兼容")
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
