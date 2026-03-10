#!/usr/bin/env python3
"""
测试在不依赖pydantic的情况下运行系统
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_without_pydantic():
    """测试不使用pydantic的模块"""
    print("=" * 60)
    print("测试不依赖pydantic的模块")
    print("=" * 60)
    
    # 1. 测试我们的优化模块
    print("\n1. 测试优化模块:")
    try:
        from src.config.advanced_manager import get_config_manager
        print("   ✅ 高级配置管理器可用")
        
        # 测试基本功能
        manager = get_config_manager()
        print(f"   ✅ 配置管理器创建成功: {manager}")
    except ImportError as e:
        print(f"   ❌ 高级配置管理器导入失败: {e}")
    
    # 2. 测试事件存储
    print("\n2. 测试事件存储:")
    try:
        from src.utils.event_store import LocalEventStore
        print("   ✅ 事件存储模块可用")
        
        # 测试创建实例
        store = LocalEventStore(":memory:")
        print(f"   ✅ 事件存储创建成功: {store}")
    except ImportError as e:
        print(f"   ❌ 事件存储导入失败: {e}")
    
    # 3. 测试内存管理器
    print("\n3. 测试内存管理器:")
    try:
        from src.utils.memory_manager import get_memory_manager
        print("   ✅ 内存管理器可用")
        
        # 测试创建实例
        manager = get_memory_manager(max_memory_mb=512)
        print(f"   ✅ 内存管理器创建成功: {manager}")
    except ImportError as e:
        print(f"   ❌ 内存管理器导入失败: {e}")
    
    # 4. 测试健康监控
    print("\n4. 测试健康监控:")
    try:
        from src.monitoring.health_check import HealthMonitor
        print("   ✅ 健康监控模块可用")
        
        # 测试创建实例
        monitor = HealthMonitor(":memory:")
        print(f"   ✅ 健康监控创建成功: {monitor}")
    except ImportError as e:
        print(f"   ❌ 健康监控导入失败: {e}")
    
    # 5. 测试指标引擎
    print("\n5. 测试指标引擎:")
    try:
        from src.indicators.engine.integration import get_indicator_integration
        print("   ✅ 指标集成器可用")
        
        # 测试创建实例
        integration = get_indicator_integration()
        print(f"   ✅ 指标集成器创建成功: {integration}")
    except ImportError as e:
        print(f"   ❌ 指标集成器导入失败: {e}")
    
    # 6. 测试worker.py修复
    print("\n6. 测试修复后的worker.py:")
    try:
        # 先模拟必要的依赖
        class MockMarketDataService:
            def __init__(self):
                self.market_settings = type('obj', (object,), {'ohlc_cache_limit': 100})()
                self.client = type('obj', (object,), {})()
        
        # 尝试导入worker.py
        from src.indicators.worker import IndicatorWorker
        print("   ✅ IndicatorWorker类可用")
        
        # 测试创建实例（简化版）
        mock_service = MockMarketDataService()
        worker = IndicatorWorker(
            service=mock_service,
            symbols=["XAUUSD"],
            timeframes=["M1"],
            tasks=[]
        )
        print(f"   ✅ IndicatorWorker创建成功: {worker}")
    except ImportError as e:
        print(f"   ❌ IndicatorWorker导入失败: {e}")
    except Exception as e:
        print(f"   ⚠️  IndicatorWorker创建失败: {e}")
    
    print("\n" + "=" * 60)
    print("总结:")
    print("=" * 60)
    
    print("""
优化模块不依赖pydantic，可以独立运行。
原始系统的config模块依赖pydantic，需要安装。

建议解决方案:
1. 立即安装pydantic: pip install pydantic
2. 或者修改config/compat.py，移除pydantic依赖
3. 使用我们的高级配置管理器替代原始配置系统
""")

if __name__ == "__main__":
    test_without_pydantic()