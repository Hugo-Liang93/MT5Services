#!/usr/bin/env python3
"""
测试指标计算的使用情况和问题
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta
import time

# 模拟OHLC数据
class MockOHLC:
    def __init__(self, symbol, timeframe, time, open, high, low, close, volume=1000):
        self.symbol = symbol
        self.timeframe = timeframe
        self.time = time
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.indicators = None

def test_indicator_flow():
    """测试指标计算流程"""
    print("=" * 60)
    print("指标计算使用情况分析")
    print("=" * 60)
    
    # 1. 检查配置文件
    print("\n1. 配置文件检查:")
    config_path = "config/indicators.ini"
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            content = f.read()
            print(f"✅ 配置文件存在: {config_path}")
            
            # 检查启用的指标
            enabled_indicators = []
            for line in content.split('\n'):
                if line.strip() and not line.strip().startswith(';'):
                    if line.strip().startswith('[') and line.strip().endswith(']'):
                        section = line.strip()[1:-1]
                        if section != 'worker':
                            enabled_indicators.append(section)
            
            print(f"   启用的指标: {len(enabled_indicators)} 个")
            for idx, indicator in enumerate(enabled_indicators, 1):
                print(f"   {idx:2d}. {indicator}")
    else:
        print(f"❌ 配置文件不存在: {config_path}")
    
    # 2. 检查指标函数实现
    print("\n2. 指标函数实现检查:")
    indicator_modules = [
        "src.indicators.mean",
        "src.indicators.momentum", 
        "src.indicators.volatility",
        "src.indicators.volume"
    ]
    
    for module_path in indicator_modules:
        try:
            module_name = module_path.split('.')[-1]
            __import__(module_path)
            print(f"✅ {module_name} 模块可用")
        except ImportError as e:
            print(f"❌ {module_name} 模块不可用: {e}")
    
    # 3. 检查数据流
    print("\n3. 数据流检查:")
    
    # 模拟数据
    mock_bars = []
    now = datetime.now()
    for i in range(50):
        bar_time = now - timedelta(minutes=i)
        bar = MockOHLC(
            symbol="XAUUSD",
            timeframe="M1",
            time=bar_time,
            open=1800 + i * 0.1,
            high=1800.5 + i * 0.1,
            low=1799.5 + i * 0.1,
            close=1800 + i * 0.1
        )
        mock_bars.append(bar)
    
    print(f"✅ 模拟数据创建: {len(mock_bars)} 条K线")
    
    # 4. 检查指标计算依赖
    print("\n4. 指标计算依赖检查:")
    
    # 检查worker.py中的关键方法
    worker_file = "src/indicators/worker.py"
    if os.path.exists(worker_file):
        with open(worker_file, 'r') as f:
            worker_content = f.read()
            
            # 检查关键方法
            methods_to_check = [
                "_compute_indicators",
                "update_tasks", 
                "_maybe_reload_config",
                "_update_local_cache"
            ]
            
            for method in methods_to_check:
                if method in worker_content:
                    print(f"✅ {method} 方法存在")
                else:
                    print(f"⚠️  {method} 方法不存在或名称有变化")
    else:
        print(f"❌ worker.py 文件不存在")
    
    # 5. 检查API集成
    print("\n5. API集成检查:")
    
    # 检查OHLCModel是否包含indicators字段
    schemas_file = "src/api/schemas.py"
    if os.path.exists(schemas_file):
        with open(schemas_file, 'r') as f:
            schemas_content = f.read()
            if "indicators: Optional[Dict[str, float]]" in schemas_content:
                print("✅ OHLCModel 包含 indicators 字段")
            else:
                print("❌ OHLCModel 不包含 indicators 字段")
    
    # 6. 潜在问题分析
    print("\n6. 潜在问题分析:")
    
    issues = []
    
    # 问题1: 指标计算是否与数据采集同步？
    print("   a. 数据同步问题:")
    print("      - 指标计算依赖已闭合的K线")
    print("      - 需要确保数据采集完成后才计算指标")
    print("      - 当前使用滑动窗口机制，应该能处理")
    
    # 问题2: 缓存一致性
    print("\n   b. 缓存一致性问题:")
    print("      - MarketDataService 缓存指标数据")
    print("      - IndicatorWorker 更新指标到缓存")
    print("      - 需要确保两者同步更新")
    
    # 问题3: 性能问题
    print("\n   c. 性能问题:")
    print("      - 每个K线都要计算所有指标")
    print("      - 指标数量增加会影响性能")
    print("      - 建议: 使用增量计算或缓存优化")
    
    # 问题4: 错误处理
    print("\n   d. 错误处理问题:")
    print("      - 单个指标计算失败不应影响其他指标")
    print("      - 需要完善的错误日志和恢复机制")
    
    # 7. 建议的改进
    print("\n7. 建议的改进措施:")
    improvements = [
        "1. 添加指标计算性能监控",
        "2. 实现指标计算失败重试机制",
        "3. 添加指标数据验证",
        "4. 优化缓存策略（LRU等）",
        "5. 添加指标计算延迟告警",
        "6. 支持指标计算优先级",
        "7. 添加指标历史数据回填监控",
        "8. 优化多时间框架指标计算"
    ]
    
    for improvement in improvements:
        print(f"   {improvement}")
    
    print("\n" + "=" * 60)
    print("总结:")
    print("=" * 60)
    
    print("""
当前指标计算系统的主要特点：
1. 配置驱动：从 indicators.ini 加载指标任务
2. 事件驱动：基于新K线触发指标计算
3. 缓存集成：指标数据存储在 MarketDataService 缓存
4. API 可用：通过 OHLCModel 的 indicators 字段返回

主要潜在问题：
1. 性能：随着指标数量增加，计算开销增大
2. 可靠性：缺乏完善的错误恢复机制
3. 监控：缺少详细的性能指标和告警
4. 一致性：缓存更新可能存在的竞态条件

建议优先解决的问题：
1. 添加性能监控和告警
2. 优化缓存策略
3. 完善错误处理机制
""")

if __name__ == "__main__":
    test_indicator_flow()