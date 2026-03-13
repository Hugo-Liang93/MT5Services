"""
指标模块 v2 测试
"""

import sys
import os
import time
from datetime import datetime, timedelta
import random

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.clients.mt5_market import OHLC


def create_mock_ohlc(count: int = 100, start_price: float = 100.0) -> list[OHLC]:
    """创建模拟的OHLC数据"""
    bars = []
    current_time = datetime.now() - timedelta(minutes=count)
    
    price = start_price
    
    for i in range(count):
        # 生成随机价格变化
        change = random.uniform(-1.0, 1.0)
        price += change
        
        # 确保价格为正
        price = max(price, 0.1)
        
        # 生成OHLC
        open_price = price
        high_price = price + abs(random.uniform(0, 0.5))
        low_price = price - abs(random.uniform(0, 0.5))
        close_price = price
        
        bar = OHLC(
            time=current_time + timedelta(minutes=i),
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            tick_volume=random.randint(100, 1000),
            spread=random.randint(1, 10),
            real_volume=random.randint(1000, 10000)
        )
        bars.append(bar)
    
    return bars


def test_smart_cache():
    """测试智能缓存系统"""
    print("=" * 60)
    print("测试智能缓存系统")
    print("=" * 60)
    
    from src.indicators_v2.base.smart_cache import SmartCache, get_global_cache
    
    # 创建缓存实例
    cache = SmartCache(maxsize=5, ttl=2)  # 2秒过期
    
    # 测试设置和获取
    cache.set("key1", "value1")
    cache.set("key2", "value2")
    cache.set("key3", "value3")
    
    assert cache.get("key1") == "value1"
    assert cache.get("key2") == "value2"
    assert cache.get("key3") == "value3"
    
    print("✓ 基本设置和获取测试通过")
    
    # 测试LRU淘汰
    cache.set("key4", "value4")
    cache.set("key5", "value5")
    cache.set("key6", "value6")  # 应该淘汰key1
    
    assert cache.get("key1") is None  # 应该被淘汰
    assert cache.get("key6") == "value6"  # 应该存在
    
    print("✓ LRU淘汰测试通过")
    
    # 测试TTL过期
    time.sleep(3)  # 等待过期
    assert cache.get("key2") is None  # 应该过期
    
    print("✓ TTL过期测试通过")
    
    # 测试统计信息
    stats = cache.get_stats()
    assert "hit_rate" in stats
    assert "size" in stats
    
    print("✓ 统计信息测试通过")
    
    # 测试全局缓存
    global_cache = get_global_cache()
    global_cache.set("global_key", "global_value")
    assert global_cache.get("global_key") == "global_value"
    
    print("✓ 全局缓存测试通过")
    
    print("\n所有智能缓存测试通过！\n")


def test_metrics_collector():
    """测试性能指标收集器"""
    print("=" * 60)
    print("测试性能指标收集器")
    print("=" * 60)
    
    from src.indicators_v2.monitoring.metrics_collector import (
        get_global_collector, record_indicator_computation
    )
    
    collector = get_global_collector(max_history=100, time_window_hours=1)
    
    # 记录一些指标计算
    for i in range(10):
        record_indicator_computation(
            name=f"indicator_{i % 3}",  # 3个不同的指标
            compute_time=random.uniform(0.001, 0.01),
            cache_hit=random.choice([True, False]),
            data_points=random.randint(10, 100),
            success=random.choice([True, True, True, False]),  # 75%成功率
            error_msg="Test error" if random.random() < 0.25 else None,
            symbol="EURUSD",
            timeframe="M1",
            incremental=random.choice([True, False])
        )
    
    # 获取报告
    report = collector.get_aggregated_report()
    
    assert "system" in report
    assert "indicators" in report
    assert len(report["indicators"]) >= 1
    
    print("✓ 基本记录和报告测试通过")
    
    # 测试性能摘要
    summary = collector.get_performance_summary()
    assert "system" in summary
    assert "top_indicators" in summary
    
    print("✓ 性能摘要测试通过")
    
    # 测试导出
    export_data = collector.export_to_json()
    assert "metadata" in export_data
    assert "aggregated_report" in export_data
    assert "recent_metrics" in export_data
    
    print("✓ 导出功能测试通过")
    
    # 测试清除
    old_size = len(collector.metrics_history)
    collector.clear_history()
    assert len(collector.metrics_history) == 0
    
    print(f"✓ 清除历史测试通过（清除了 {old_size} 条记录）")
    
    print("\n所有性能指标收集器测试通过！\n")


def test_incremental_base():
    """测试增量计算基类"""
    print("=" * 60)
    print("测试增量计算基类")
    print("=" * 60)
    
    from src.indicators_v2.base.incremental import IncrementalIndicator, SimpleIndicator
    
    # 创建模拟数据
    bars = create_mock_ohlc(50)
    
    # 测试简单指标（不支持增量）
    class TestSimpleIndicator(SimpleIndicator):
        def _compute_full(self, bars):
            # 简单计算：返回平均值
            closes = [bar.close for bar in bars]
            avg = sum(closes) / len(closes) if closes else 0
            return {"avg": avg}
    
    simple_indicator = TestSimpleIndicator("test_simple", {"min_bars": 10})
    
    # 计算
    result = simple_indicator.compute(bars, "EURUSD", "M1")
    assert "avg" in result
    assert result["avg"] > 0
    
    print("✓ 简单指标计算测试通过")
    
    # 测试增量指标
    class TestIncrementalIndicator(IncrementalIndicator):
        def _compute_full(self, bars):
            # 完整计算：返回平均值和最大值
            closes = [bar.close for bar in bars]
            avg = sum(closes) / len(closes) if closes else 0
            max_val = max(closes) if closes else 0
            return {"avg": avg, "max": max_val}
        
        def _compute_incremental(self, bars, state):
            # 简单的增量计算：假设我们知道如何增量更新
            # 这里为了测试，我们回退到完整计算
            return self._compute_full(bars)
    
    incremental_indicator = TestIncrementalIndicator("test_incremental", {"min_bars": 10})
    
    # 第一次计算（完整计算）
    result1 = incremental_indicator.compute(bars[:30], "EURUSD", "M1")
    assert "avg" in result1
    assert "max" in result1
    
    # 第二次计算（应该尝试增量，但数据不连续）
    result2 = incremental_indicator.compute(bars[20:40], "EURUSD", "M1")
    assert "avg" in result2
    
    print("✓ 增量指标计算测试通过")
    
    # 测试状态管理
    state = incremental_indicator.get_state("EURUSD", "M1")
    assert state is not None
    assert state.data_points > 0
    
    print("✓ 状态管理测试通过")
    
    # 测试清除状态
    count = incremental_indicator.clear_state("EURUSD", "M1")
    assert count >= 1
    
    state_after_clear = incremental_indicator.get_state("EURUSD", "M1")
    assert state_after_clear is None
    
    print("✓ 状态清除测试通过")
    
    print("\n所有增量计算基类测试通过！\n")


def test_integration():
    """测试集成功能"""
    print("=" * 60)
    print("测试集成功能")
    print("=" * 60)
    
    # 创建模拟数据
    bars = create_mock_ohlc(100)
    
    # 测试缓存和监控的集成
    from src.indicators_v2.base.smart_cache import get_global_cache
    from src.indicators_v2.monitoring.metrics_collector import get_global_collector
    
    cache = get_global_cache()
    collector = get_global_collector()
    
    # 使用缓存
    cache_key = "test_integration_key"
    cache_value = {"data": bars[:10], "result": 123.45}
    
    cache.set(cache_key, cache_value)
    retrieved = cache.get(cache_key)
    assert retrieved == cache_value
    
    # 记录指标计算
    from src.indicators_v2.monitoring.metrics_collector import record_indicator_computation
    
    for i in range(5):
        record_indicator_computation(
            name="integration_test",
            compute_time=0.005,
            cache_hit=True,
            data_points=50,
            symbol="EURUSD",
            timeframe="M1",
            incremental=(i % 2 == 0)
        )
    
    # 检查统计
    stats = cache.get_stats()
    assert stats["hits"] >= 1
    
    report = collector.get_aggregated_report()
    assert "integration_test" in report["indicators"]
    
    print("✓ 缓存和监控集成测试通过")
    
    print("\n所有集成测试通过！\n")


def run_all_tests():
    """运行所有测试"""
    print("开始运行指标模块 v2 测试")
    print("=" * 60)
    
    try:
        test_smart_cache()
        test_metrics_collector()
        test_incremental_base()
        test_integration()
        
        print("=" * 60)
        print("🎉 所有测试通过！")
        print("=" * 60)
        
        return True
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)