# 配置驱动指标管理器 - 快速入门

## 🎯 一句话总结
**不再需要理解多个模块，只需编辑配置文件，系统自动处理一切。**

## 📋 核心文件

| 文件 | 用途 | 重要性 |
|------|------|--------|
| `config/indicators_v2.json` | **主配置文件**，管理所有指标 | ⭐⭐⭐⭐⭐ |
| `src/indicators_v2/manager.py` | **统一管理器**，唯一入口点 | ⭐⭐⭐⭐⭐ |
| `src/api/indicators.py` | **API端点**，提供HTTP访问 | ⭐⭐⭐⭐ |

## 🚀 5分钟上手

### 第一步：查看当前配置
```bash
# 查看配置文件
cat config/indicators_v2.json

# 或使用API查看
curl http://localhost:8808/indicators/list
```

### 第二步：添加新指标
编辑 `config/indicators_v2.json`，在 `indicators` 数组中添加：
```json
{
  "name": "my_sma",
  "func_path": "src.indicators.mean.sma",
  "params": {"period": 30},
  "dependencies": [],
  "compute_mode": "standard",
  "enabled": true,
  "description": "我的30周期SMA"
}
```

### 第三步：使用指标
```python
# 代码中使用
from src.indicators_v2.manager import get_global_unified_manager

manager = get_global_unified_manager(market_service)
indicators = manager.get_all_indicators("EURUSD", "M1")
print(indicators["my_sma"])

# 或通过API
# GET http://localhost:8808/indicators/EURUSD/M1/my_sma
```

## 🔧 常用操作

### 1. 修改指标参数
```json
// 修改 config/indicators_v2.json
"params": {"period": 25}  // 从30改为25
```
**系统自动**：60秒内检测变化，重新计算，更新缓存。

### 2. 临时禁用指标
```json
"enabled": false  // 从true改为false
```
**系统自动**：停止计算，清理缓存，其他指标不受影响。

### 3. 添加指标依赖
```json
"dependencies": ["sma20", "ema50"]  // 依赖其他指标
```
**系统自动**：更新依赖图，确保计算顺序正确。

## 📊 API快速参考

### 获取数据
```bash
# 获取所有指标
GET /indicators/{symbol}/{timeframe}

# 获取单个指标
GET /indicators/{symbol}/{timeframe}/{indicator_name}

# 实时计算
POST /indicators/compute
{
  "symbol": "EURUSD",
  "timeframe": "M1",
  "indicators": ["sma20", "rsi14"]
}
```

### 系统管理
```bash
# 查看性能统计
GET /indicators/performance/stats

# 清空缓存
POST /indicators/cache/clear

# 查看依赖关系
GET /indicators/dependency/graph?format=mermaid
```

## 💡 最佳实践

### 1. 配置文件管理
```json
{
  "indicators": [
    // 按类别分组，方便管理
    // 趋势指标
    {"name": "sma20", "tags": ["trend"], ...},
    {"name": "ema50", "tags": ["trend"], ...},
    
    // 动量指标
    {"name": "rsi14", "tags": ["momentum"], ...},
    {"name": "macd", "tags": ["momentum"], ...},
    
    // 波动率指标
    {"name": "boll20", "tags": ["volatility"], ...},
    {"name": "atr14", "tags": ["volatility"], ...}
  ]
}
```

### 2. 计算模式选择
```json
{
  "compute_mode": "standard",      // 标准计算（大多数指标）
  "compute_mode": "incremental",   // 增量计算（EMA、ATR等）
  "compute_mode": "parallel"       // 并行计算（复杂指标）
}
```

### 3. 缓存策略
```json
{
  "cache_strategy": "lru_ttl",     // 推荐：LRU+TTL双重策略
  "cache_ttl": 300.0,              // 缓存5分钟
  "cache_maxsize": 1000            // 最多缓存1000项
}
```

## 🔄 与传统方式对比

### 传统方式（复杂）
```python
# 需要导入多个模块
from src.indicators.worker import IndicatorWorker
from src.indicators.engine.integration import IndicatorIntegration

# 手动初始化
worker = IndicatorWorker(...)
integration = IndicatorIntegration(...)

# 手动处理交互
worker.start()
integration.register_indicators()
```

### 新方式（简单）
```python
# 只需一个导入
from src.indicators_v2.manager import get_global_unified_manager

# 自动从配置文件初始化
manager = get_global_unified_manager(market_service)

# 简单使用
indicators = manager.get_all_indicators("EURUSD", "M1")
```

## 🚨 常见问题

### Q1: 修改配置后多久生效？
**A**: 默认60秒内自动检测并生效（可配置 `reload_interval`）。

### Q2: 如何添加自定义指标函数？
**A**: 
1. 在 `src/indicators/` 下创建函数
2. 在配置文件中引用函数路径
3. 系统自动加载并开始计算

### Q3: 指标计算失败怎么办？
**A**: 
1. 检查 `GET /indicators/performance/stats` 查看错误率
2. 检查日志中的详细错误信息
3. 确保数据足够（满足 `min_bars` 要求）

### Q4: 如何监控系统状态？
**A**: 
- `GET /indicators/performance/stats` - 性能统计
- `GET /monitoring/health` - 系统健康状态
- 查看日志文件中的详细记录

## 📈 性能优化建议

### 1. 调整并行度
```json
{
  "pipeline": {
    "max_workers": 4,  // 根据CPU核心数调整
    "enable_parallel": true
  }
}
```

### 2. 优化缓存
```json
{
  "pipeline": {
    "cache_ttl": 60.0,     // 高频交易可缩短TTL
    "cache_maxsize": 5000  // 内存充足可增加缓存大小
  }
}
```

### 3. 选择计算模式
- **增量计算**：适合EMA、ATR等可增量更新的指标
- **并行计算**：适合MACD等复杂计算
- **标准计算**：适合简单指标

## 🎯 总结

### 你只需要记住：
1. **编辑配置文件** (`config/indicators_v2.json`)
2. **使用统一管理器** (`UnifiedIndicatorManager`)
3. **通过API访问** (`/indicators/*` 端点)

### 系统自动处理：
- ✅ 依赖关系管理
- ✅ 并行计算调度
- ✅ 智能缓存管理
- ✅ 性能监控统计
- ✅ 配置热重载
- ✅ 错误处理和重试

### 开始使用：
```python
# 最简单的使用方式
from src.indicators_v2.manager import get_global_unified_manager

manager = get_global_unified_manager()
indicators = manager.get_all_indicators("EURUSD", "M1")
```

**现在就去编辑配置文件，享受配置驱动的便利吧！**