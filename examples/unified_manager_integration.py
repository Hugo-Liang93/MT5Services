#!/usr/bin/env python3
"""
统一指标管理器集成示例

展示如何在现有系统中集成和使用配置驱动的统一指标管理器
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import time
from datetime import datetime
from typing import Dict, Any


def example_basic_integration():
    """示例1: 基本集成"""
    print("=" * 60)
    print("示例1: 基本集成 - 在现有系统中使用统一管理器")
    print("=" * 60)
    
    print("""
场景: 你有一个现有的交易系统，需要集成指标计算功能。

传统方式:
1. 需要理解多个模块（worker, integration, pipeline等）
2. 手动初始化各个组件
3. 处理复杂的依赖关系
4. 配置分散在代码和文件中

新方式（配置驱动）:
1. 编辑配置文件
2. 使用统一管理器
3. 系统自动处理一切
""")
    
    # 模拟现有系统的市场服务
    class ExistingMarketService:
        def __init__(self):
            self.name = "现有市场服务"
        
        def get_data(self, symbol, timeframe):
            # 模拟获取数据
            return [{"close": 1.1000 + i*0.0001} for i in range(100)]
    
    print("\n集成步骤:")
    print("1. 确保配置文件存在: config/indicators_v2.json")
    print("2. 在系统初始化时创建统一管理器")
    print("3. 在需要指标的地方使用管理器")
    
    print("""
代码示例:

# 在系统初始化时
from src.indicators_v2.manager import get_global_unified_manager

class TradingSystem:
    def __init__(self, market_service):
        # 初始化统一管理器
        self.indicator_manager = get_global_unified_manager(
            market_service=market_service,
            config_file="config/indicators_v2.json"
        )
        print("✅ 指标管理器初始化完成")
    
    def make_decision(self, symbol, timeframe):
        # 获取指标
        indicators = self.indicator_manager.get_all_indicators(symbol, timeframe)
        
        # 基于指标做决策
        if indicators.get('rsi14', {}).get('rsi', 50) > 70:
            return "超买，考虑卖出"
        elif indicators.get('rsi14', {}).get('rsi', 50) < 30:
            return "超卖，考虑买入"
        
        return "持有"
    
    def add_custom_indicator(self):
        # 动态添加指标
        from src.indicators_v2.config import IndicatorConfig, ComputeMode
        
        new_indicator = IndicatorConfig(
            name="custom_ma",
            func_path="src.indicators.mean.sma",
            params={"period": 25},
            enabled=True
        )
        
        self.indicator_manager.add_indicator(new_indicator)
        print("✅ 自定义指标已添加")
""")
    
    print("✅ 基本集成示例完成")


def example_configuration_management():
    """示例2: 配置管理"""
    print("\n" + "=" * 60)
    print("示例2: 配置管理 - 通过配置文件控制所有行为")
    print("=" * 60)
    
    print("""
配置文件 (config/indicators_v2.json) 控制所有行为:

1. 指标定义: 名称、函数、参数、依赖
2. 计算参数: 并行度、缓存策略、重试次数
3. 系统行为: 自动启动、热重载、监控
4. 数据范围: 交易品种、时间框架
""")
    
    config_example = {
        "indicators": [
            {
                "name": "trend_sma",
                "func_path": "src.indicators.mean.sma",
                "params": {"period": 20},
                "dependencies": [],
                "compute_mode": "standard",
                "enabled": True,
                "description": "趋势判断用SMA",
                "tags": ["trend"]
            },
            {
                "name": "momentum_rsi",
                "func_path": "src.indicators.momentum.rsi",
                "params": {"period": 14},
                "dependencies": ["trend_sma"],  # RSI依赖SMA
                "compute_mode": "standard",
                "enabled": True,
                "description": "动量判断用RSI",
                "tags": ["momentum"]
            }
        ],
        "pipeline": {
            "enable_parallel": True,
            "max_workers": 4,
            "enable_cache": True,
            "cache_strategy": "lru_ttl",
            "cache_ttl": 300.0,
            "enable_monitoring": True
        },
        "symbols": ["EURUSD", "GBPUSD", "XAUUSD"],
        "timeframes": ["M1", "M5", "H1"],
        "auto_start": True,
        "hot_reload": True
    }
    
    print(f"\n配置示例 (JSON格式):")
    print(json.dumps(config_example, indent=2, ensure_ascii=False))
    
    print("""
配置说明:

1. 指标配置:
   - name: 指标名称（在API和代码中使用）
   - func_path: 函数路径（系统自动导入）
   - params: 参数（传递给函数）
   - dependencies: 依赖关系（自动处理计算顺序）
   - compute_mode: 计算模式（standard/incremental/parallel）
   - enabled: 是否启用（可动态开关）

2. 流水线配置:
   - enable_parallel: 启用并行计算
   - max_workers: 最大工作线程数
   - enable_cache: 启用缓存
   - cache_strategy: 缓存策略
   - enable_monitoring: 启用监控

3. 系统配置:
   - symbols: 计算的交易品种
   - timeframes: 计算的时间框架
   - auto_start: 是否自动启动
   - hot_reload: 是否支持热重载
""")
    
    print("✅ 配置管理示例完成")


def example_dynamic_operations():
    """示例3: 动态操作"""
    print("\n" + "=" * 60)
    print("示例3: 动态操作 - 运行时管理指标")
    print("=" * 60)
    
    print("""
动态操作能力:
1. 运行时添加新指标
2. 修改现有指标参数
3. 启用/禁用指标
4. 移除不再需要的指标
5. 更新配置并热重载
""")
    
    print("""
代码示例:

from src.indicators_v2.manager import get_global_unified_manager
from src.indicators_v2.config import IndicatorConfig, ComputeMode

# 获取管理器
manager = get_global_unified_manager()

# 1. 添加新指标（运行时）
print("添加新指标...")
new_indicator = IndicatorConfig(
    name="dynamic_ema",
    func_path="src.indicators.mean.ema",
    params={"period": 30},
    compute_mode=ComputeMode.INCREMENTAL,
    enabled=True,
    description="动态添加的EMA"
)
manager.add_indicator(new_indicator)

# 2. 更新指标参数
print("更新指标参数...")
manager.update_indicator(
    "sma20",
    params={"period": 25},  # 从20改为25
    description="更新为25周期SMA"
)

# 3. 禁用指标
print("禁用指标...")
manager.update_indicator("ema50", enabled=False)

# 4. 启用指标
print("启用指标...")
manager.update_indicator("ema50", enabled=True)

# 5. 移除指标
print("移除指标...")
manager.remove_indicator("dynamic_ema")

# 6. 查看指标信息
print("查看指标信息...")
info = manager.get_indicator_info("sma20")
print(f"指标信息: {info}")

# 7. 列出所有指标
print("列出所有指标...")
indicators = manager.list_indicators()
print(f"共有 {len(indicators)} 个指标")

# 8. 获取性能统计
print("获取性能统计...")
stats = manager.get_performance_stats()
print(f"缓存命中率: {stats.get('cache_hit_rate', 'N/A')}")

# 9. 清空缓存
print("清空缓存...")
cache_count = manager.clear_cache()
print(f"清除了 {cache_count} 个缓存项")

# 10. 获取依赖关系图
print("获取依赖关系图...")
graph = manager.get_dependency_graph("mermaid")
print(f"依赖图大小: {len(graph)} 字符")
""")
    
    print("✅ 动态操作示例完成")


def example_api_usage():
    """示例4: API使用"""
    print("\n" + "=" * 60)
    print("示例4: API使用 - 通过HTTP访问指标")
    print("=" * 60)
    
    print("""
API端点提供HTTP访问，方便其他系统集成:

主要端点:
1. GET  /indicators/list                    # 列出所有指标
2. GET  /indicators/{symbol}/{timeframe}    # 获取所有指标值
3. GET  /indicators/{symbol}/{timeframe}/{name}  # 获取单个指标
4. POST /indicators/compute                 # 实时计算
5. GET  /indicators/performance/stats       # 性能统计
6. POST /indicators/cache/clear             # 清空缓存
7. GET  /indicators/dependency/graph        # 依赖关系图
""")
    
    print("""
使用示例:

# 1. 使用curl命令行
print("使用curl访问API...")
print('''
# 列出所有指标
curl http://localhost:8808/indicators/list

# 获取EURUSD M1的所有指标
curl http://localhost:8808/indicators/EURUSD/M1

# 获取单个指标
curl http://localhost:8808/indicators/EURUSD/M1/sma20

# 实时计算
curl -X POST http://localhost:8808/indicators/compute \\
  -H "Content-Type: application/json" \\
  -d '{"symbol": "GBPUSD", "timeframe": "H1", "indicators": ["rsi14", "macd"]}'

# 性能统计
curl http://localhost:8808/indicators/performance/stats
''')

# 2. 使用Python requests库
print("使用Python requests库...")
print('''
import requests

# 获取指标
response = requests.get("http://localhost:8808/indicators/EURUSD/M1")
if response.status_code == 200:
    data = response.json()
    if data["success"]:
        indicators = data["data"]
        print(f"获取到 {len(indicators)} 个指标")
        
# 实时计算
payload = {
    "symbol": "GBPUSD",
    "timeframe": "H1",
    "indicators": ["rsi14", "macd"]
}
response = requests.post(
    "http://localhost:8808/indicators/compute",
    json=payload
)
''')

# 3. 在Web应用中使用
print("在Web应用中使用...")
print('''
# 前端JavaScript
async function getIndicators(symbol, timeframe) {
    const response = await fetch(`/indicators/${symbol}/${timeframe}`);
    const data = await response.json();
    return data.data;
}

# 显示指标
async function displayIndicators() {
    const indicators = await getIndicators("EURUSD", "M1");
    for (const [name, value] of Object.entries(indicators)) {
        console.log(`${name}: ${JSON.stringify(value)}`);
    }
}
''')
""")
    
    print("✅ API使用示例完成")


def example_migration_from_old_system():
    """示例5: 从旧系统迁移"""
    print("\n" + "=" * 60)
    print("示例5: 迁移指南 - 从传统方式迁移到配置驱动")
    print("=" * 60)
    
    print("""
迁移步骤:

1. 备份现有配置
2. 创建新的配置文件
3. 更新代码使用统一管理器
4. 测试验证
5. 逐步迁移
""")
    
    print("""
详细迁移指南:

# 步骤1: 备份现有配置
print("备份现有配置...")
print('''
# 备份现有的indicators.ini
cp config/indicators.ini config/indicators.ini.backup

# 备份相关代码
cp src/indicators/worker.py src/indicators/worker.py.backup
''')

# 步骤2: 创建新的配置文件
print("创建新的配置文件...")
print('''
# 基于现有配置创建新的JSON配置
# 转换现有的INI配置到JSON格式

现有INI配置示例:
[sma20]
func = src.indicators.mean.sma
params = {"period": 20, "min_bars": 20}

转换为JSON:
{
  "name": "sma20",
  "func_path": "src.indicators.mean.sma",
  "params": {"period": 20, "min_bars": 20},
  "enabled": true
}
''')

# 步骤3: 更新代码
print("更新代码使用统一管理器...")
print('''
# 旧代码
from src.indicators.worker import IndicatorWorker
from src.config import load_indicator_settings

settings = load_indicator_settings()
worker = IndicatorWorker(settings)
worker.start()

# 新代码
from src.indicators_v2.manager import get_global_unified_manager

manager = get_global_unified_manager(market_service)
# 自动从配置文件启动，无需手动调用start()
''')

# 步骤4: 测试验证
print("测试验证...")
print('''
# 验证指标计算
indicators = manager.get_all_indicators("EURUSD", "M1")
print(f"获取到 {len(indicators)} 个指标")

# 验证API
import requests
response = requests.get("http://localhost:8808/indicators/list")
print(f"API响应: {response.status_code}")

# 验证配置热重载
# 修改配置文件，观察是否自动生效
''')

# 步骤5: 逐步迁移
print("逐步迁移策略...")
print('''
策略1: 并行运行
- 新旧系统同时运行
- 对比结果一致性
- 逐步切换流量

策略2: 功能逐步迁移
- 先迁移简单指标
- 再迁移复杂指标
- 最后迁移依赖关系

策略3: 按业务模块迁移
- 先迁移趋势指标
- 再迁移动量指标
- 最后迁移波动率指标
''')
""")
    
    print("✅ 迁移指南示例完成")


def main():
    """主函数"""
    print("🚀 统一指标管理器集成示例")
    print("=" * 60)
    print("展示配置驱动指标管理器的完整使用场景")
    print("=" * 60)
    
    examples = [
        ("基本集成", example_basic_integration),
        ("配置管理", example_configuration_management),
        ("动态操作", example_dynamic_operations),
        ("API使用", example_api_usage),
        ("迁移指南", example_migration_from_old_system),
    ]
    
    for example_name, example_func in examples:
        print(f"\n▶️ 运行示例: {example_name}")
        try:
            example_func()
        except Exception as e:
            print(f"❌ 示例运行异常: {e}")
            import traceback
            traceback.print_exc()
    
    print("\n" + "=" * 60)
    print("🎉 所有示例完成!")
    print("=" * 60)
    
    print("""
总结:

配置驱动的统一指标管理器提供:
✅ 简化使用: 只需编辑配置文件，使用统一管理器
✅ 强大功能: 依赖管理、并行计算、智能缓存、性能监控
✅ 灵活扩展: 动态添加/修改/删除指标，配置热重载
✅ 易于集成: 简单API，支持HTTP访问，易于与其他系统集成
✅ 平滑迁移: 提供从传统系统迁移的完整指南

下一步行动:
1. 查看配置文件: config/indicators_v2.json
2. 运行演示脚本: python demo_config_driven.py
3. 查看快速入门: cat QUICK_START.md
4. 开始集成到你的系统中!
""")


if __name__ == "__main__":
    main()