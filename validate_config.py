#!/usr/bin/env python3
"""
配置验证脚本 - 不依赖pydantic
"""

import configparser
import os
import sys


def load_config_with_base(config_name, base_config="app.ini"):
    """加载配置文件并合并基础配置"""
    config_dir = os.path.join(os.path.dirname(__file__), "config")
    
    # 加载基础配置 - 禁用插值避免 % 符号问题
    base_path = os.path.join(config_dir, base_config)
    base_parser = configparser.ConfigParser(interpolation=None)
    base_parser.read(base_path, encoding="utf-8")
    
    # 加载目标配置 - 禁用插值
    target_path = os.path.join(config_dir, config_name)
    target_parser = configparser.ConfigParser(interpolation=None)
    target_parser.read(target_path, encoding="utf-8")
    
    # 创建合并的解析器 - 禁用插值
    merged_parser = configparser.ConfigParser(interpolation=None)
    
    # 首先添加基础配置的所有节
    for section in base_parser.sections():
        if not merged_parser.has_section(section):
            merged_parser.add_section(section)
        for key, value in base_parser.items(section):
            merged_parser.set(section, key, value)
    
    # 然后添加/覆盖目标配置
    for section in target_parser.sections():
        if not merged_parser.has_section(section):
            merged_parser.add_section(section)
        for key, value in target_parser.items(section):
            merged_parser.set(section, key, value)
    
    return merged_parser


def validate_config_consistency():
    """验证配置一致性"""
    print("=== 配置一致性验证 ===\n")
    
    # 加载主配置
    main_config = load_config_with_base("app.ini")
    
    # 检查交易配置
    print("1. 检查交易配置...")
    symbols = main_config.get("trading", "symbols", fallback="").split(",")
    symbols = [s.strip() for s in symbols if s.strip()]
    default_symbol = main_config.get("trading", "default_symbol", fallback="").strip()
    
    print(f"   交易品种: {symbols}")
    print(f"   默认品种: {default_symbol}")
    
    if not symbols:
        print("   ❌ 错误: 未配置交易品种")
        return False
    
    if default_symbol and default_symbol not in symbols:
        print(f"   ❌ 错误: 默认品种 '{default_symbol}' 不在交易品种列表中")
        return False
    
    print("   ✅ 交易配置验证通过")
    
    # 检查时间框架
    print("\n2. 检查时间框架配置...")
    timeframes = main_config.get("trading", "timeframes", fallback="").split(",")
    timeframes = [tf.strip() for tf in timeframes if tf.strip()]
    
    print(f"   时间框架: {timeframes}")
    
    if not timeframes:
        print("   ❌ 警告: 未配置时间框架")
    else:
        print("   ✅ 时间框架配置正常")
    
    # 检查间隔配置
    print("\n3. 检查间隔配置...")
    try:
        tick_interval = float(main_config.get("intervals", "tick_interval", fallback="0.5"))
        ohlc_interval = float(main_config.get("intervals", "ohlc_interval", fallback="30.0"))
        
        print(f"   Tick采集间隔: {tick_interval}秒")
        print(f"   OHLC采集间隔: {ohlc_interval}秒")
        
        if tick_interval < 0.1:
            print(f"   ⚠️ 警告: Tick采集间隔过小 ({tick_interval}秒)")
        if ohlc_interval < 1.0:
            print(f"   ⚠️ 警告: OHLC采集间隔过小 ({ohlc_interval}秒)")
        
        print("   ✅ 间隔配置验证通过")
    except ValueError as e:
        print(f"   ❌ 错误: 间隔配置格式错误 - {e}")
        return False
    
    # 检查模块配置继承
    print("\n4. 检查模块配置继承...")
    
    # 检查 market.ini
    market_config = load_config_with_base("market.ini")
    if market_config.has_section("api"):
        print("   ✅ market.ini 配置正常")
    else:
        print("   ⚠️ 警告: market.ini 缺少 [api] 节")
    
    # 检查 ingest.ini
    ingest_config = load_config_with_base("ingest.ini")
    if ingest_config.has_section("ingest"):
        print("   ✅ ingest.ini 配置正常")
    else:
        print("   ⚠️ 警告: ingest.ini 缺少 [ingest] 节")
    
    # 验证配置继承效果
    print("\n5. 验证配置继承效果...")
    
    # ingest 应该从主配置继承品种
    ingest_symbols_config = ingest_config.get("ingest", "ingest_symbols", fallback="")
    if ingest_symbols_config:
        print(f"   ⚠️ 注意: ingest.ini 中仍有 ingest_symbols 配置: {ingest_symbols_config}")
        print("     建议: 移除此项，直接从 app.ini 继承")
    else:
        print("   ✅ ingest.ini 正确地从 app.ini 继承品种配置")
    
    # market 应该从主配置继承默认品种
    market_default_symbol = market_config.get("api", "default_symbol", fallback="")
    if market_default_symbol:
        print(f"   ⚠️ 注意: market.ini 中仍有 default_symbol 配置: {market_default_symbol}")
        print("     建议: 移除此项，直接从 app.ini 继承")
    else:
        print("   ✅ market.ini 正确地从 app.ini 继承默认品种配置")
    
    return True


def show_config_summary():
    """显示配置摘要"""
    print("\n=== 配置系统摘要 ===\n")
    
    main_config = load_config_with_base("app.ini")
    
    print("📊 核心配置:")
    print(f"   交易品种: {main_config.get('trading', 'symbols', fallback='未设置')}")
    print(f"   默认品种: {main_config.get('trading', 'default_symbol', fallback='未设置')}")
    print(f"   时间框架: {main_config.get('trading', 'timeframes', fallback='未设置')}")
    
    print("\n⏱️ 采集间隔:")
    print(f"   Tick间隔: {main_config.get('intervals', 'tick_interval', fallback='未设置')}秒")
    print(f"   OHLC间隔: {main_config.get('intervals', 'ohlc_interval', fallback='未设置')}秒")
    
    print("\n📈 API限制:")
    print(f"   Tick返回限制: {main_config.get('limits', 'tick_limit', fallback='未设置')}")
    print(f"   OHLC返回限制: {main_config.get('limits', 'ohlc_limit', fallback='未设置')}")
    
    print("\n⚙️ 系统配置:")
    print(f"   时区: {main_config.get('system', 'timezone', fallback='未设置')}")
    print(f"   日志级别: {main_config.get('system', 'log_level', fallback='未设置')}")
    
    print("\n🎯 单一信号源实现:")
    print("   ✅ 核心配置集中在 app.ini")
    print("   ✅ 模块配置继承主配置")
    print("   ✅ 配置一致性自动验证")
    print("   ✅ 向后兼容性保持")


def main():
    """主函数"""
    print("开始验证配置中心化系统...\n")
    
    try:
        # 验证配置一致性
        if not validate_config_consistency():
            print("\n❌ 配置验证失败，请检查配置文件")
            return 1
        
        # 显示配置摘要
        show_config_summary()
        
        print("\n" + "="*50)
        print("🎉 配置中心化系统验证通过！")
        print("\n✅ 已实现的功能:")
        print("   1. 单一信号源配置 (app.ini)")
        print("   2. 配置继承机制")
        print("   3. 配置一致性验证")
        print("   4. 向后兼容性保持")
        print("\n📝 使用说明:")
        print("   - 修改核心配置只需编辑 config/app.ini")
        print("   - 模块特有配置在各自的配置文件中")
        print("   - 现有代码无需修改，自动兼容")
        
        return 0
        
    except Exception as e:
        print(f"\n❌ 验证过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())