"""
指标模块 v2 第二阶段测试
测试依赖关系管理和并行计算框架
"""

import sys
import os
import time
from datetime import datetime, timedelta
import random
from dataclasses import dataclass

# 模拟OHLC类
@dataclass
class MockOHLC:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = 0
    spread: int = 0
    real_volume: int = 0


def create_mock_ohlc(count: int = 100, start_price: float = 100.0) -> list[MockOHLC]:
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
        
        bar = MockOHLC(
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


def test_dependency_manager():
    """测试依赖关系管理器"""
    print("=" * 60)
    print("测试依赖关系管理器")
    print("=" * 60)
    
    # 动态导入
    import importlib.util
    import sys
    
    spec = importlib.util.spec_from_file_location(
        "dependency_manager",
        "src/indicators_v2/engine/dependency_manager.py"
    )
    dep_module = importlib.util.module_from_spec(spec)
    sys.modules["dependency_manager"] = dep_module
    spec.loader.exec_module(dep_module)
    
    from dependency_manager import DependencyManager, get_global_dependency_manager
    
    # 创建依赖管理器
    dm = DependencyManager()
    
    # 定义一些模拟指标函数
    def sma_func(bars, period=20):
        closes = [bar.close for bar in bars]
        return {"sma": sum(closes) / len(closes)} if closes else {}
    
    def ema_func(bars, period=20):
        closes = [bar.close for bar in bars]
        return {"ema": sum(closes) / len(closes)} if closes else {}
    
    def rsi_func(bars, period=14):
        # 简单模拟
        return {"rsi": 50.0}
    
    def macd_func(bars, fast=12, slow=26, signal=9):
        # 简单模拟
        return {"macd": 0.1, "signal": 0.05, "hist": 0.05}
    
    # 添加指标（无依赖）
    dm.add_indicator("sma20", sma_func, {"period": 20})
    dm.add_indicator("ema50", ema_func, {"period": 50})
    
    print("✓ 基本指标添加测试通过")
    
    # 添加有依赖的指标
    dm.add_indicator("rsi14", rsi_func, {"period": 14}, dependencies=["sma20"])
    dm.add_indicator("macd", macd_func, {"fast": 12, "slow": 26, "signal": 9}, 
                    dependencies=["ema50", "rsi14"])
    
    print("✓ 依赖指标添加测试通过")
    
    # 测试依赖获取
    deps = dm.get_dependencies("macd")
    assert "ema50" in deps
    assert "rsi14" in deps
    assert len(deps) == 2
    
    print("✓ 依赖获取测试通过")
    
    # 测试执行顺序
    execution_order = dm.get_execution_order(["macd"])
    assert len(execution_order) >= 2  # 至少2层：基础指标 + macd
    
    # 第一层应该包含基础指标
    first_level = execution_order[0]
    assert "sma20" in first_level or "ema50" in first_level
    
    print("✓ 执行顺序测试通过")
    
    # 测试并行组
    parallel_groups = dm.get_parallelizable_groups(["macd", "rsi14", "sma20"])
    assert len(parallel_groups) >= 1
    
    print("✓ 并行组测试通过")
    
    # 测试验证
    errors = dm.validate_graph()
    assert len(errors) == 0
    
    print("✓ 图验证测试通过")
    
    # 测试可视化
    mermaid = dm.visualize("mermaid")
    assert "graph TD" in mermaid
    assert "sma20" in mermaid or "ema50" in mermaid
    
    print("✓ 可视化测试通过")
    
    # 测试全局实例
    global_dm = get_global_dependency_manager()
    global_dm.add_indicator("test_global", sma_func, {"period": 10})
    assert "test_global" in global_dm.indicator_funcs
    
    print("✓ 全局实例测试通过")
    
    print("\n所有依赖关系管理器测试通过！\n")


def test_parallel_executor():
    """测试并行计算框架"""
    print("=" * 60)
    print("测试并行计算框架")
    print("=" * 60)
    
    # 动态导入
    import importlib.util
    import sys
    
    spec = importlib.util.spec_from_file_location(
        "parallel_executor",
        "src/indicators_v2/engine/parallel_executor.py"
    )
    parallel_module = importlib.util.module_from_spec(spec)
    sys.modules["parallel_executor"] = parallel_module
    spec.loader.exec_module(parallel_module)
    
    from parallel_executor import ParallelExecutor, TaskStatus, execute_parallel_tasks
    
    # 测试函数
    def slow_func(x, delay=0.1):
        time.sleep(delay)
        return x * 2
    
    def fast_func(x):
        return x + 1
    
    def failing_func(x):
        if x < 0:
            raise ValueError("Negative input")
        return x * 3
    
    # 创建执行器
    executor = ParallelExecutor(max_workers=2, max_retries=1, enable_cache=True)
    
    # 测试基本任务提交
    task_id1 = executor.submit_task(slow_func, args=(10,), kwargs={"delay": 0.05})
    task_id2 = executor.submit_task(fast_func, args=(20,))
    
    assert task_id1 is not None
    assert task_id2 is not None
    
    print("✓ 任务提交测试通过")
    
    # 测试任务结果获取
    result1 = executor.get_task_result(task_id1, timeout=1.0)
    result2 = executor.get_task_result(task_id2, timeout=1.0)
    
    assert result1 is not None
    assert result2 is not None
    assert result1.success
    assert result2.success
    assert result1.result == 20  # 10 * 2
    assert result2.result == 21  # 20 + 1
    
    print("✓ 任务结果获取测试通过")
    
    # 测试缓存
    task_id3 = executor.submit_task(slow_func, args=(30,), kwargs={"delay": 0.05}, use_cache=True)
    result3 = executor.get_task_result(task_id3, timeout=1.0)
    assert result3.success
    assert result3.result == 60  # 30 * 2
    
    # 再次提交相同任务（应该命中缓存）
    task_id4 = executor.submit_task(slow_func, args=(30,), kwargs={"delay": 0.05}, use_cache=True)
    result4 = executor.get_task_result(task_id4, timeout=0.1)  # 应该立即返回
    
    assert result4 is not None
    assert result4.success
    
    print("✓ 缓存测试通过")
    
    # 测试失败任务
    task_id5 = executor.submit_task(failing_func, args=(-5,))
    result5 = executor.get_task_result(task_id5, timeout=1.0)
    
    assert result5 is not None
    assert not result5.success
    assert result5.status == TaskStatus.FAILED
    assert "Negative input" in str(result5.error)
    
    print("✓ 失败任务测试通过")
    
    # 测试并行执行多个任务
    tasks = [
        (slow_func, (1,), {"delay": 0.02}),
        (slow_func, (2,), {"delay": 0.02}),
        (slow_func, (3,), {"delay": 0.02}),
        (slow_func, (4,), {"delay": 0.02}),
    ]
    
    start_time = time.time()
    results = executor.execute_parallel(tasks, use_cache=False)
    end_time = time.time()
    
    # 检查结果
    assert len(results) == 4
    for task_id, task_result in results.items():
        assert task_result is not None
        assert task_result.success
    
    # 并行执行应该比串行快
    # 4个任务，每个0.02秒，串行需要0.08秒，并行应该更快
    parallel_time = end_time - start_time
    assert parallel_time < 0.06  # 应该小于串行时间
    
    print(f"✓ 并行执行测试通过（{parallel_time*1000:.1f}ms，预期<60ms）")
    
    # 测试统计信息
    stats = executor.get_stats()
    assert "total_tasks" in stats
    assert "cache_hits" in stats
    assert "success_rate" in stats
    
    print("✓ 统计信息测试通过")
    
    # 测试便捷函数
    tasks_simple = [
        (fast_func, (10,), {}),
        (fast_func, (20,), {}),
        (fast_func, (30,), {}),
    ]
    
    simple_results = execute_parallel_tasks(tasks_simple, max_workers=2)
    assert len(simple_results) == 3
    
    print("✓ 便捷函数测试通过")
    
    # 清理
    executor.shutdown()
    
    print("\n所有并行计算框架测试通过！\n")


def test_optimized_pipeline():
    """测试优化计算流水线"""
    print("=" * 60)
    print("测试优化计算流水线")
    print("=" * 60)
    
    # 动态导入
    import importlib.util
    import sys
    
    # 先导入依赖模块
    spec_dep = importlib.util.spec_from_file_location(
        "dependency_manager",
        "src/indicators_v2/engine/dependency_manager.py"
    )
    dep_module = importlib.util.module_from_spec(spec_dep)
    sys.modules["dependency_manager"] = dep_module
    spec_dep.loader.exec_module(dep_module)
    
    spec_parallel = importlib.util.spec_from_file_location(
        "parallel_executor",
        "src/indicators_v2/engine/parallel_executor.py"
    )
    parallel_module = importlib.util.module_from_spec(spec_parallel)
    sys.modules["parallel_executor"] = parallel_module
    spec_parallel.loader.exec_module(parallel_module)
    
    spec_pipeline = importlib.util.spec_from_file_location(
        "pipeline_v2",
        "src/indicators_v2/engine/pipeline_v2.py"
    )
    pipeline_module = importlib.util.module_from_spec(spec_pipeline)
    sys.modules["pipeline_v2"] = pipeline_module
    spec_pipeline.loader.exec_module(pipeline_module)
    
    from pipeline_v2 import OptimizedPipeline, PipelineConfig
    
    # 创建模拟数据
    bars = create_mock_ohlc(50)
    
    # 定义指标函数
    def sma_func(bars, period=20):
        closes = [bar.close for bar in bars[-period:]] if len(bars) >= period else [bar.close for bar in bars]
        avg = sum(closes) / len(closes) if closes else 0
        return {"sma": avg}
    
    def ema_func(bars, period=20):
        # 简单EMA计算
        closes = [bar.close for bar in bars[-period:]] if len(bars) >= period else [bar.close for bar in bars]
        if not closes:
            return {"ema": 0}
        
        k = 2 / (period + 1)
        ema_val = closes[0]
        for price in closes[1:]:
            ema_val = ema_val + k * (price - ema_val)
        return {"ema": ema_val}
    
    def rsi_func(bars, period=14, sma20=None):
        # 使用SMA作为基础（模拟依赖）
        if isinstance(sma20, dict):
            # 提取SMA值
            base = sma20.get("sma", 50.0)
        else:
            base = sma20 if sma20 is not None else 50.0
        rsi_val = max(0, min(100, 50 + (base - 50) * 0.5))
        return {"rsi": rsi_val}
    
    def macd_func(bars, fast=12, slow=26, signal=9, ema50=None, rsi14=None):
        # 使用EMA和RSI作为基础（模拟依赖）
        # 提取EMA值
        if isinstance(ema50, dict):
            ema_base = ema50.get("ema", 100.0)
        else:
            ema_base = ema50 if ema50 is not None else 100.0
        
        # 提取RSI值
        if isinstance(rsi14, dict):
            rsi_base = rsi14.get("rsi", 50.0)
        else:
            rsi_base = rsi14 if rsi14 is not None else 50.0
        
        macd_val = ema_base * 0.01
        signal_val = macd_val * 0.9
        hist_val = macd_val - signal_val
        
        # 加入RSI影响
        macd_val *= (rsi_base / 50.0)
        signal_val *= (rsi_base / 50.0)
        
        return {
            "macd": macd_val,
            "signal": signal_val,
            "hist": hist_val
        }
    
    # 创建流水线配置
    config = PipelineConfig(
        enable_parallel=True,
        max_workers=2,
        enable_cache=True,
        cache_ttl=10.0,  # 短TTL便于测试
        enable_incremental=False,  # 简化测试
        enable_monitoring=False
    )
    
    # 创建流水线
    pipeline = OptimizedPipeline(config)
    
    # 注册指标
    pipeline.register_indicator("sma20", sma_func, {"period": 20})
    pipeline.register_indicator("ema50", ema_func, {"period": 50})
    pipeline.register_indicator("rsi14", rsi_func, {"period": 14}, dependencies=["sma20"])
    pipeline.register_indicator("macd", macd_func, {"fast": 12, "slow": 26, "signal": 9}, 
                              dependencies=["ema50", "rsi14"])
    
    print("✓ 流水线初始化测试通过")
    
    # 测试执行计划
    plan = pipeline.get_execution_plan(["macd", "rsi14"])
    assert "levels" in plan
    assert "execution_groups" in plan
    assert len(plan["execution_groups"]) >= 2
    
    print("✓ 执行计划测试通过")
    
    # 测试计算
    results = pipeline.compute("EURUSD", "M1", bars, ["sma20", "ema50", "rsi14", "macd"])
    
    assert "sma20" in results
    assert "ema50" in results
    assert "rsi14" in results
    assert "macd" in results
    
    # 检查结果格式
    sma_result = results["sma20"]
    assert isinstance(sma_result, dict)
    assert "sma" in sma_result
    assert sma_result["sma"] > 0
    
    macd_result = results["macd"]
    assert "macd" in macd_result
    assert "signal" in macd_result
    assert "hist" in macd_result
    
    print("✓ 流水线计算测试通过")
    
    # 测试单个指标计算
    single_result = pipeline.compute_single("sma20", "EURUSD", "M1", bars)
    assert single_result is not None
    assert "sma" in single_result
    
    print("✓ 单个指标计算测试通过")
    
    # 测试缓存
    # 第一次计算
    start_time1 = time.time()
    results1 = pipeline.compute("EURUSD", "M1", bars, ["sma20", "ema50"])
    time1 = time.time() - start_time1
    
    # 第二次计算（应该命中缓存）
    start_time2 = time.time()
    results2 = pipeline.compute("EURUSD", "M1", bars, ["sma20", "ema50"])
    time2 = time.time() - start_time2
    
    # 缓存应该更快
    assert time2 < time1 * 0.5  # 缓存应该至少快50%
    
    print(f"✓ 缓存优化测试通过（第一次：{time1*1000:.1f}ms，第二次：{time2*1000:.1f}ms）")
    
    # 测试统计信息
    stats = pipeline.get_stats()
    assert "total_computations" in stats
    assert "cache" in stats
    assert "success_rate" in stats
    
    print("✓ 统计信息测试通过")
    
    # 测试清理
    cache_count = pipeline.clear_cache()
    assert cache_count >= 0
    
    print("✓ 缓存清理测试通过")
    
    # 关闭流水线
    pipeline.shutdown()
    
    print("\n所有优化计算流水线测试通过！\n")


def test_integration():
    """测试集成功能"""
    print("=" * 60)
    print("测试集成功能")
    print("=" * 60)
    
    # 测试所有模块的集成
    print("测试智能缓存、性能监控、依赖管理、并行计算的集成...")
    
    # 创建模拟数据
    bars = create_mock_ohlc(30)
    
    # 导入所有模块
    import importlib.util
    import sys
    
    # 导入基础模块
    spec_cache = importlib.util.spec_from_file_location(
        "smart_cache",
        "src/indicators_v2/base/smart_cache.py"
    )
    cache_module = importlib.util.module_from_spec(spec_cache)
    sys.modules["smart_cache"] = cache_module
    spec_cache.loader.exec_module(cache_module)
    
    spec_metrics = importlib.util.spec_from_file_location(
        "metrics_collector",
        "src/indicators_v2/monitoring/metrics_collector.py"
    )
    metrics_module = importlib.util.module_from_spec(spec_metrics)
    sys.modules["metrics_collector"] = metrics_module
    spec_metrics.loader.exec_module(metrics_module)
    
    spec_dep = importlib.util.spec_from_file_location(
        "dependency_manager",
        "src/indicators_v2/engine/dependency_manager.py"
    )
    dep_module = importlib.util.module_from_spec(spec_dep)
    sys.modules["dependency_manager"] = dep_module
    spec_dep.loader.exec_module(dep_module)
    
    spec_parallel = importlib.util.spec_from_file_location(
        "parallel_executor",
        "src/indicators_v2/engine/parallel_executor.py"
    )
    parallel_module = importlib.util.module_from_spec(spec_parallel)
    sys.modules["parallel_executor"] = parallel_module
    spec_parallel.loader.exec_module(parallel_module)
    
    spec_pipeline = importlib.util.spec_from_file_location(
        "pipeline_v2",
        "src/indicators_v2/engine/pipeline_v2.py"
    )
    pipeline_module = importlib.util.module_from_spec(spec_pipeline)
    sys.modules["pipeline_v2"] = pipeline_module
    spec_pipeline.loader.exec_module(pipeline_module)
    
    from smart_cache import get_global_cache
    from metrics_collector import get_global_collector, record_indicator_computation, get_performance_report
    from dependency_manager import get_global_dependency_manager
    from parallel_executor import get_global_executor
    from pipeline_v2 import get_global_pipeline, PipelineConfig
    
    # 初始化全局组件
    cache = get_global_cache(maxsize=100, ttl=5)
    collector = get_global_collector(max_history=100, time_window_hours=1)
    dep_manager = get_global_dependency_manager()
    executor = get_global_executor(max_workers=2, enable_cache=True)
    
    # 定义测试函数
    def test_func(x):
        time.sleep(0.01)  # 模拟计算
        return x * 2
    
    def indicator_func(bars):
        closes = [bar.close for bar in bars]
        avg = sum(closes) / len(closes) if closes else 0
        return {"value": avg}
    
    # 测试缓存
    cache.set("test_key", "test_value")
    assert cache.get("test_key") == "test_value"
    
    # 测试性能监控
    for i in range(5):
        record_indicator_computation(
            name=f"test_indicator_{i}",
            compute_time=0.005,
            cache_hit=(i % 2 == 0),
            data_points=30,
            symbol="EURUSD",
            timeframe="M1",
            incremental=False
        )
    
    report = get_performance_report()
    assert "system" in report
    assert "indicators" in report
    
    # 测试依赖管理
    dep_manager.add_indicator("base_indicator", indicator_func, {"param": 1})
    dep_manager.add_indicator("dependent_indicator", indicator_func, {"param": 2}, 
                            dependencies=["base_indicator"])
    
    execution_order = dep_manager.get_execution_order(["dependent_indicator"])
    assert len(execution_order) >= 2
    
    # 测试并行执行
    tasks = [
        (test_func, (1,), {}),
        (test_func, (2,), {}),
        (test_func, (3,), {}),
    ]
    
    results = executor.execute_parallel(tasks, use_cache=True)
    assert len(results) == 3
    
    # 测试流水线
    config = PipelineConfig(
        enable_parallel=True,
        max_workers=2,
        enable_cache=True,
        enable_monitoring=True
    )
    
    pipeline = get_global_pipeline(config)
    
    # 注册指标到流水线
    pipeline.register_indicator("test_sma", indicator_func, {"period": 20})
    
    # 计算
    results = pipeline.compute("TEST", "M1", bars, ["test_sma"])
    assert "test_sma" in results
    
    # 获取统计
    stats = pipeline.get_stats()
    assert "total_computations" in stats
    assert "cache" in stats
    
    print("✓ 所有模块集成测试通过")
    
    # 清理
    cache.clear()
    collector.clear_history()
    dep_manager.clear()
    executor.shutdown(wait=True)
    
    print("\n所有集成测试通过！\n")


def run_all_tests():
    """运行所有测试"""
    print("开始运行指标模块 v2 第二阶段测试")
    print("=" * 60)
    
    try:
        test_dependency_manager()
        test_parallel_executor()
        test_optimized_pipeline()
        test_integration()
        
        print("=" * 60)
        print("🎉 所有第二阶段测试通过！")
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