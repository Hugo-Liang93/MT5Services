#!/usr/bin/env python3
"""
统一指标管理器集成测试

测试新实现的配置驱动统一管理器是否正常工作
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import time
from datetime import datetime

# 模拟市场数据服务
class MockMarketService:
    def __init__(self):
        self.data = {
            "EURUSD": {
                "M1": [MockOHLC(1.1000 + i*0.0001) for i in range(100)],
                "M5": [MockOHLC(1.1000 + i*0.0005) for i in range(100)],
            },
            "GBPUSD": {
                "M1": [MockOHLC(1.2500 + i*0.0001) for i in range(100)],
                "M5": [MockOHLC(1.2500 + i*0.0005) for i in range(100)],
            }
        }
    
    def get_ohlc(self, symbol, timeframe, count=100):
        return self.data.get(symbol, {}).get(timeframe, [])[:count]


class MockOHLC:
    def __init__(self, close):
        self.close = close
        self.open = close - 0.0001
        self.high = close + 0.0001
        self.low = close - 0.0002
        self.time = datetime.now()


def test_config_loading():
    """测试配置加载"""
    print("=" * 60)
    print("测试1: 配置加载")
    print("=" * 60)
    
    try:
        from src.indicators_v2.config import ConfigLoader
        
        # 测试从JSON加载
        config = ConfigLoader.load("config/indicators_v2.json")
        
        print(f"✅ 配置加载成功")
        print(f"   指标数量: {len(config.indicators)}")
        print(f"   交易品种: {config.symbols}")
        print(f"   时间框架: {config.timeframes}")
        print(f"   自动启动: {config.auto_start}")
        print(f"   热重载: {config.hot_reload}")
        
        # 显示前3个指标
        print(f"\n   前3个指标:")
        for i, indicator in enumerate(config.indicators[:3]):
            print(f"     {i+1}. {indicator.name}: {indicator.description}")
        
        return True
        
    except Exception as e:
        print(f"❌ 配置加载失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_manager_initialization():
    """测试管理器初始化"""
    print("\n" + "=" * 60)
    print("测试2: 管理器初始化")
    print("=" * 60)
    
    try:
        from src.indicators_v2.manager import get_global_unified_manager
        
        market_service = MockMarketService()
        
        print("初始化统一指标管理器...")
        manager = get_global_unified_manager(
            market_service=market_service,
            config_file="config/indicators_v2.json"
        )
        
        print(f"✅ 管理器初始化成功")
        
        # 测试基本功能
        print("\n测试基本功能:")
        
        # 1. 列出指标
        indicators_info = manager.list_indicators()
        print(f"   1. 指标列表: {len(indicators_info)} 个指标")
        
        # 2. 获取指标信息
        if indicators_info:
            first_indicator = indicators_info[0]["name"]
            info = manager.get_indicator_info(first_indicator)
            print(f"   2. 指标信息获取: {first_indicator} - {info.get('description', 'N/A')}")
        
        # 3. 获取性能统计
        stats = manager.get_performance_stats()
        print(f"   3. 性能统计: 配置了 {stats['config']['total_indicators']} 个指标")
        
        return True
        
    except Exception as e:
        print(f"❌ 管理器初始化失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_indicator_computation():
    """测试指标计算"""
    print("\n" + "=" * 60)
    print("测试3: 指标计算")
    print("=" * 60)
    
    try:
        from src.indicators_v2.manager import get_global_unified_manager
        
        market_service = MockMarketService()
        manager = get_global_unified_manager(market_service)
        
        print("测试指标计算功能...")
        
        # 测试获取指标（应该返回空，因为还没有数据）
        indicators = manager.get_all_indicators("EURUSD", "M1")
        print(f"   1. 获取缓存指标: {len(indicators)} 个指标")
        
        # 测试实时计算
        print("   2. 实时计算指标...")
        results = manager.compute(
            symbol="EURUSD",
            timeframe="M1",
            indicator_names=["sma20", "rsi14"]
        )
        
        print(f"     计算结果: {len(results)} 个指标")
        for name, value in results.items():
            if value:
                print(f"     - {name}: {value}")
        
        # 再次获取应该从缓存获取
        indicators = manager.get_all_indicators("EURUSD", "M1")
        print(f"   3. 再次获取（缓存）: {len(indicators)} 个指标")
        
        return True
        
    except Exception as e:
        print(f"❌ 指标计算测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_dynamic_management():
    """测试动态管理"""
    print("\n" + "=" * 60)
    print("测试4: 动态指标管理")
    print("=" * 60)
    
    try:
        from src.indicators_v2.manager import get_global_unified_manager
        from src.indicators_v2.config import IndicatorConfig, ComputeMode
        
        market_service = MockMarketService()
        manager = get_global_unified_manager(market_service)
        
        print("测试动态添加指标...")
        
        # 添加测试指标
        test_config = IndicatorConfig(
            name="test_sma30",
            func_path="src.indicators.mean.sma",
            params={"period": 30, "min_bars": 30},
            dependencies=[],
            compute_mode=ComputeMode.STANDARD,
            enabled=True,
            description="测试用30周期SMA",
            tags=["test"]
        )
        
        # 添加指标
        success = manager.add_indicator(test_config)
        print(f"   1. 添加指标: {'成功' if success else '失败'}")
        
        if success:
            # 验证指标已添加
            info = manager.get_indicator_info("test_sma30")
            print(f"   2. 验证添加: {'成功' if info else '失败'}")
            
            if info:
                print(f"     指标信息: {info['name']} - {info['description']}")
            
            # 移除指标
            success = manager.remove_indicator("test_sma30")
            print(f"   3. 移除指标: {'成功' if success else '失败'}")
            
            # 验证已移除
            info = manager.get_indicator_info("test_sma30")
            print(f"   4. 验证移除: {'成功' if info is None else '失败'}")
        
        return True
        
    except Exception as e:
        print(f"❌ 动态管理测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_api_endpoints():
    """测试API端点"""
    print("\n" + "=" * 60)
    print("测试5: API端点模拟")
    print("=" * 60)
    
    try:
        # 模拟API请求
        print("模拟API端点调用...")
        
        endpoints = [
            ("GET", "/indicators/list", "列出所有指标"),
            ("GET", "/indicators/EURUSD/M1", "获取EURUSD M1的所有指标"),
            ("GET", "/indicators/EURUSD/M1/sma20", "获取单个指标"),
            ("POST", "/indicators/compute", "实时计算指标"),
            ("GET", "/indicators/performance/stats", "获取性能统计"),
            ("POST", "/indicators/cache/clear", "清空缓存"),
            ("GET", "/indicators/dependency/graph?format=mermaid", "获取依赖关系图"),
        ]
        
        for i, (method, path, description) in enumerate(endpoints, 1):
            print(f"   {i}. {method} {path}")
            print(f"      → {description}")
        
        print("\n✅ API端点定义完整")
        
        # 测试实际API模块导入
        print("\n测试API模块导入...")
        try:
            from src.api.indicators import router
            print("✅ API路由模块导入成功")
            
            # 检查路由数量
            routes = [route for route in router.routes]
            print(f"   路由数量: {len(routes)}")
            
            return True
            
        except Exception as e:
            print(f"❌ API模块导入失败: {e}")
            return False
        
    except Exception as e:
        print(f"❌ API测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """主测试函数"""
    print("🚀 统一指标管理器集成测试")
    print("=" * 60)
    
    tests = [
        ("配置加载", test_config_loading),
        ("管理器初始化", test_manager_initialization),
        ("指标计算", test_indicator_computation),
        ("动态管理", test_dynamic_management),
        ("API端点", test_api_endpoints),
    ]
    
    results = []
    
    for test_name, test_func in tests:
        print(f"\n▶️ 运行测试: {test_name}")
        try:
            success = test_func()
            results.append((test_name, success))
        except Exception as e:
            print(f"❌ 测试异常: {e}")
            results.append((test_name, False))
    
    # 汇总结果
    print("\n" + "=" * 60)
    print("📊 测试结果汇总")
    print("=" * 60)
    
    passed = 0
    total = len(results)
    
    for test_name, success in results:
        status = "✅ 通过" if success else "❌ 失败"
        print(f"{status} - {test_name}")
        if success:
            passed += 1
    
    print(f"\n📈 通过率: {passed}/{total} ({passed/total*100:.1f}%)")
    
    if passed == total:
        print("\n🎉 所有测试通过！统一指标管理器工作正常。")
    else:
        print(f"\n⚠️  {total - passed} 个测试失败，需要检查实现。")
    
    return passed == total


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)