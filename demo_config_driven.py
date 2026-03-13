#!/usr/bin/env python3
"""
配置驱动指标管理器演示

展示：
1. 如何通过配置文件管理指标
2. 配置热重载功能
3. 动态指标管理
4. 简化后的API使用
"""

import json
import time
import threading
from datetime import datetime
from pathlib import Path

# 模拟市场数据服务
class MockMarketService:
    def __init__(self):
        self.data = {
            "EURUSD": {
                "M1": [{"close": 1.1000 + i*0.0001} for i in range(100)],
                "M5": [{"close": 1.1000 + i*0.0005} for i in range(100)],
                "H1": [{"close": 1.1000 + i*0.0010} for i in range(100)]
            },
            "GBPUSD": {
                "M1": [{"close": 1.2500 + i*0.0001} for i in range(100)],
                "M5": [{"close": 1.2500 + i*0.0005} for i in range(100)]
            }
        }
    
    def get_ohlc(self, symbol, timeframe, count=100):
        return self.data.get(symbol, {}).get(timeframe, [])[:count]


def demo_basic_usage():
    """演示基本使用"""
    print("=" * 60)
    print("演示1: 基本使用")
    print("=" * 60)
    
    # 创建模拟市场服务
    market_service = MockMarketService()
    
    try:
        # 导入统一管理器
        from src.indicators_v2.manager import get_global_unified_manager
        
        # 初始化管理器（自动从配置文件加载）
        print("1. 初始化统一指标管理器...")
        manager = get_global_unified_manager(
            market_service=market_service,
            config_file="config/indicators_v2.json"
        )
        
        # 获取指标列表
        print("\n2. 获取指标列表...")
        indicators_info = manager.list_indicators()
        print(f"   配置了 {len(indicators_info)} 个指标:")
        for info in indicators_info:
            print(f"   - {info['name']}: {info['description']}")
        
        # 获取指标值
        print("\n3. 获取指标值...")
        for symbol in ["EURUSD", "GBPUSD"]:
            for timeframe in ["M1", "M5"]:
                indicators = manager.get_all_indicators(symbol, timeframe)
                if indicators:
                    print(f"   {symbol}/{timeframe}: {len(indicators)} 个指标")
                    for name, value in list(indicators.items())[:2]:  # 只显示前2个
                        print(f"     {name}: {value}")
        
        # 获取性能统计
        print("\n4. 获取性能统计...")
        stats = manager.get_performance_stats()
        print(f"   配置统计:")
        print(f"     - 总指标数: {stats['config']['total_indicators']}")
        print(f"     - 启用指标: {stats['config']['enabled_indicators']}")
        print(f"     - 交易品种: {stats['config']['symbols']}")
        print(f"     - 时间框架: {stats['config']['timeframes']}")
        
        print("\n✅ 基本使用演示完成")
        
    except Exception as e:
        print(f"❌ 演示失败: {e}")
        import traceback
        traceback.print_exc()


def demo_config_hot_reload():
    """演示配置热重载"""
    print("\n" + "=" * 60)
    print("演示2: 配置热重载")
    print("=" * 60)
    
    # 备份原始配置文件
    config_path = Path("config/indicators_v2.json")
    backup_path = Path("config/indicators_v2.json.backup")
    
    if config_path.exists():
        import shutil
        shutil.copy(config_path, backup_path)
        print("1. 已备份原始配置文件")
    
    try:
        # 导入配置管理器
        from src.indicators_v2.config import get_global_config_manager
        
        # 创建配置管理器
        config_manager = get_global_config_manager("config/indicators_v2.json")
        
        # 获取当前配置
        config = config_manager.get_config()
        original_count = len(config.indicators)
        print(f"2. 当前有 {original_count} 个指标")
        
        # 修改配置文件（添加新指标）
        print("3. 修改配置文件（添加新指标）...")
        config_data = json.loads(config_path.read_text())
        
        # 添加新指标
        new_indicator = {
            "name": "demo_sma30",
            "func_path": "src.indicators.mean.sma",
            "params": {
                "period": 30,
                "min_bars": 30
            },
            "dependencies": [],
            "compute_mode": "standard",
            "enabled": true,
            "description": "演示用30周期SMA",
            "tags": ["demo", "trend"]
        }
        
        config_data["indicators"].append(new_indicator)
        
        # 保存修改
        config_path.write_text(json.dumps(config_data, indent=2, ensure_ascii=False))
        print("   配置文件已更新")
        
        # 等待热重载
        print("4. 等待配置热重载（60秒间隔）...")
        print("   注意：实际系统中，修改会立即被检测到")
        
        # 模拟热重载
        time.sleep(2)  # 模拟等待
        
        # 手动触发重载
        if config_manager.reload():
            print("✅ 配置热重载成功")
            new_config = config_manager.get_config()
            print(f"   现在有 {len(new_config.indicators)} 个指标")
            
            # 检查新指标是否已添加
            new_indicator_names = [ind.name for ind in new_config.indicators]
            if "demo_sma30" in new_indicator_names:
                print("   ✅ 新指标 'demo_sma30' 已成功添加")
            else:
                print("   ❌ 新指标未找到")
        else:
            print("❌ 配置热重载失败")
        
        print("\n✅ 配置热重载演示完成")
        
    except Exception as e:
        print(f"❌ 演示失败: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        # 恢复原始配置文件
        if backup_path.exists():
            shutil.move(backup_path, config_path)
            print("5. 已恢复原始配置文件")


def demo_dynamic_management():
    """演示动态指标管理"""
    print("\n" + "=" * 60)
    print("演示3: 动态指标管理")
    print("=" * 60)
    
    try:
        # 导入必要的模块
        from src.indicators_v2.manager import get_global_unified_manager
        from src.indicators_v2.config import IndicatorConfig, ComputeMode
        
        # 创建模拟市场服务
        market_service = MockMarketService()
        
        # 获取管理器
        manager = get_global_unified_manager(market_service)
        
        # 1. 添加新指标
        print("1. 动态添加新指标...")
        new_config = IndicatorConfig(
            name="dynamic_ema25",
            func_path="src.indicators.mean.ema",
            params={"period": 25, "min_bars": 25},
            dependencies=[],
            compute_mode=ComputeMode.INCREMENTAL,
            enabled=True,
            description="动态添加的25周期EMA",
            tags=["dynamic", "trend"]
        )
        
        success = manager.add_indicator(new_config)
        if success:
            print("   ✅ 指标添加成功")
            
            # 验证指标已添加
            info = manager.get_indicator_info("dynamic_ema25")
            if info:
                print(f"   指标信息: {info['name']} - {info['description']}")
                print(f"   计算模式: {info['compute_mode']}")
                print(f"   参数: {info['params']}")
        else:
            print("   ❌ 指标添加失败")
        
        # 2. 更新现有指标
        print("\n2. 动态更新指标...")
        success = manager.update_indicator(
            "sma20",
            params={"period": 22, "min_bars": 22},
            description="更新为22周期SMA"
        )
        
        if success:
            print("   ✅ 指标更新成功")
            
            # 验证更新
            info = manager.get_indicator_info("sma20")
            if info and info["params"].get("period") == 22:
                print(f"   参数已更新: period={info['params'].get('period')}")
        else:
            print("   ❌ 指标更新失败")
        
        # 3. 禁用指标
        print("\n3. 动态禁用指标...")
        success = manager.update_indicator("ema50", enabled=False)
        if success:
            print("   ✅ 指标禁用成功")
            
            # 验证禁用
            info = manager.get_indicator_info("ema50")
            if info and not info["enabled"]:
                print("   ema50 已禁用")
        else:
            print("   ❌ 指标禁用失败")
        
        # 4. 移除指标
        print("\n4. 动态移除指标...")
        success = manager.remove_indicator("dynamic_ema25")
        if success:
            print("   ✅ 指标移除成功")
            
            # 验证移除
            info = manager.get_indicator_info("dynamic_ema25")
            if info is None:
                print("   dynamic_ema25 已成功移除")
        else:
            print("   ❌ 指标移除失败")
        
        print("\n✅ 动态指标管理演示完成")
        
    except Exception as e:
        print(f"❌ 演示失败: {e}")
        import traceback
        traceback.print_exc()


def demo_api_integration():
    """演示API集成"""
    print("\n" + "=" * 60)
    print("演示4: API集成")
    print("=" * 60)
    
    try:
        # 模拟API请求
        print("1. 模拟API端点调用...")
        
        # 列出所有指标
        print("\n   GET /indicators/list")
        print("   → 返回所有可用的指标名称")
        
        # 获取指标值
        print("\n   GET /indicators/EURUSD/M1")
        print("   → 返回EURUSD M1的所有指标值")
        
        print("\n   GET /indicators/EURUSD/M1/sma20")
        print("   → 返回EURUSD M1的sma20指标值")
        
        # 实时计算
        print("\n   POST /indicators/compute")
        print("   → 请求体: {\"symbol\": \"GBPUSD\", \"timeframe\": \"H1\", \"indicators\": [\"rsi14\", \"macd\"]}")
        print("   → 返回实时计算结果")
        
        # 系统管理
        print("\n   GET /indicators/performance/stats")
        print("   → 返回性能统计信息")
        
        print("\n   POST /indicators/cache/clear")
        print("   → 清空所有缓存")
        
        print("\n   GET /indicators/dependency/graph?format=mermaid")
        print("   → 返回依赖关系图（Mermaid格式）")
        
        print("\n2. API响应示例:")
        print("""
   {
     "success": true,
     "data": {
       "sma20": {"sma": 1.12345},
       "rsi14": {"rsi": 65.43},
       "macd": {"macd": 0.0012, "signal": 0.0008, "histogram": 0.0004}
     },
     "metadata": {
       "symbol": "EURUSD",
       "timeframe": "M1",
       "timestamp": "2026-03-13T02:14:00Z"
     }
   }
        """)
        
        print("\n3. 实际代码集成:")
        print("""
   # 在策略中使用
   from src.api.deps import get_unified_indicator_manager
   
   async def trading_decision(symbol: str, timeframe: str):
       manager = get_unified_indicator_manager()
       indicators = manager.get_all_indicators(symbol, timeframe)
       
       # 基于指标做决策
       rsi = indicators.get('rsi14', {}).get('rsi', 50)
       if rsi > 70:
           return "SELL"
       elif rsi < 30:
           return "BUY"
       return "HOLD"
        """)
        
        print("\n✅ API集成演示完成")
        
    except Exception as e:
        print(f"❌ 演示失败: {e}")
        import traceback
        traceback.print_exc()


def main():
    """主函数"""
    print("🚀 配置驱动指标管理器演示")
    print("=" * 60)
    print("这个演示展示如何通过配置文件统一管理指标模块")
    print("=" * 60)
    
    # 运行所有演示
    demo_basic_usage()
    demo_config_hot_reload()
    demo_dynamic_management()
    demo_api_integration()
    
    print("\n" + "=" * 60)
    print("🎉 所有演示完成!")
    print("=" * 60)
    print("\n总结:")
    print("1. ✅ 通过配置文件统一管理所有指标")
    print("2. ✅ 配置热重载，修改实时生效")
    print("3. ✅ 动态指标管理，运行时添加/修改/删除")
    print("4. ✅ 简化API集成，单一入口点")
    print("5. ✅ 自动性能监控和依赖管理")
    print("\n现在你可以:")
    print("1. 编辑 config/indicators_v2.json 来管理指标")
    print("2. 使用 UnifiedIndicatorManager 单一接口")
    print("3. 通过API轻松访问所有功能")
    print("4. 享受配置驱动的便利性!")


if __name__ == "__main__":
    main()