# 配置中心化系统使用指南

## 🎯 概述

配置中心化系统实现了**单一信号源（Single Source of Truth）**的设计目标。所有核心配置集中在 `config/app.ini` 中，模块配置文件自动继承主配置。

## 📁 新的配置文件结构

```
config/
├── app.ini              # 🎯 主配置（单一信号源）
├── market.ini           # API特有配置（继承主配置）
├── ingest.ini           # 采集特有配置（继承主配置）
├── mt5.ini              # MT5连接配置
├── db.ini               # 数据库配置
├── cache.ini            # 缓存配置
├── storage.ini          # 存储配置
└── indicators.ini       # 指标配置
```

## 🔧 如何使用

### 1. 修改核心配置
只需编辑 `config/app.ini`：

```ini
[trading]
symbols = XAUUSD          # 修改交易品种
timeframes = M1,H1,D1     # 添加更多时间框架
default_symbol = XAUUSD   # 设置默认品种

[intervals]
tick_interval = 1.0       # 调整Tick采集频率
ohlc_interval = 60.0      # 调整OHLC采集频率

[limits]
tick_limit = 500          # 调整API返回限制
ohlc_limit = 500
```

### 2. 模块特有配置
各模块的特有配置仍在各自的文件中：

- `market.ini` - API服务特有配置（端口、CORS等）
- `ingest.ini` - 数据采集特有配置（重试策略、超时等）
- `mt5.ini` - MT5连接配置
- `db.ini` - 数据库连接配置

### 3. 代码访问方式

#### 新方式（推荐）
```python
from src.config import get_trading_config, get_interval_config

# 获取交易配置
trading = get_trading_config()
print(trading.symbols)      # ['XAUUSD']
print(trading.timeframes)   # ['M1', 'H1']
print(trading.default_symbol)  # 'XAUUSD'

# 获取间隔配置
intervals = get_interval_config()
print(intervals.tick_interval)  # 0.5
print(intervals.ohlc_interval)  # 30.0
```

#### 旧方式（完全兼容）
```python
from src.config import load_ingest_settings, load_market_settings

# 这些函数自动从主配置获取共享值
ingest = load_ingest_settings()
print(ingest.ingest_symbols)      # 从 app.ini 获取
print(ingest.ingest_tick_interval) # 从 app.ini 获取

market = load_market_settings()
print(market.default_symbol)      # 从 app.ini 获取
print(market.tick_limit)          # 从 app.ini 获取
```

## ✅ 配置验证

### 自动验证
系统自动验证配置一致性：
- 默认品种必须在交易品种列表中
- 采集间隔必须合理
- 配置格式必须正确

### 手动验证
运行验证脚本：
```bash
python3 validate_config.py
```

### 验证配置一致性
```python
from src.config import validate_config_consistency

valid, message = validate_config_consistency()
if valid:
    print("配置验证通过")
else:
    print(f"配置验证失败: {message}")
```

## 🔄 配置热重载

### 重新加载配置
```python
from src.config import reload_configs

# 修改配置文件后调用
reload_configs()
```

### 获取原始配置（兼容性）
```python
from src.config import get_raw_config

# 获取原始模块配置
mt5_config = get_raw_config("mt5")
db_config = get_raw_config("db")
```

## 🎯 针对单一品种交易的优化

### 当前配置（XAUUSD）
```ini
[trading]
symbols = XAUUSD          # 单一品种
timeframes = M1,H1        # 常用时间框架
default_symbol = XAUUSD   # 默认品种

[intervals]
tick_interval = 0.5       # 黄金波动大，较高频率
ohlc_interval = 30.0      # 适中频率

[limits]
tick_limit = 200          # 较大限制
ohlc_limit = 200
```

### 扩展为多品种
```ini
[trading]
symbols = XAUUSD,EURUSD,USDJPY,GBPUSD
timeframes = M1,M5,M15,H1,H4,D1
default_symbol = XAUUSD
```

## 🚀 快速开始

### 1. 首次使用
```bash
# 验证配置系统
python3 validate_config.py

# 查看配置摘要
python3 validate_config.py
```

### 2. 修改配置
1. 编辑 `config/app.ini`
2. 运行验证脚本确认无误
3. 重启应用或调用 `reload_configs()`

### 3. 添加新模块
1. 在 `config/` 目录创建模块配置文件
2. 使用 `load_config_with_base()` 加载配置
3. 确保共享配置从 `app.ini` 继承

## 📊 配置继承示例

### 主配置 (app.ini)
```ini
[trading]
symbols = XAUUSD
timeframes = M1,H1
default_symbol = XAUUSD
```

### 模块配置 (ingest.ini)
```ini
[ingest]
# 从 app.ini 继承 symbols, timeframes
tick_initial_lookback_seconds = 20
ohlc_backfill_limit = 500
```

### 实际效果
```python
# ingest 配置包含：
# - symbols: XAUUSD (从 app.ini 继承)
# - timeframes: M1,H1 (从 app.ini 继承)
# - tick_initial_lookback_seconds: 20 (特有)
# - ohlc_backfill_limit: 500 (特有)
```

## ⚠️ 注意事项

### 1. 配置格式
- 使用 `%%` 转义 `%` 符号（如日志格式）
- 列表使用逗号分隔：`symbols = XAUUSD,EURUSD`
- 布尔值使用：`true/false`, `yes/no`, `1/0`

### 2. 向后兼容性
- 现有代码无需修改
- 所有旧API接口保持原样
- 逐步迁移到新接口

### 3. 性能考虑
- 配置使用缓存，首次加载后快速访问
- 热重载会清除缓存，重新加载所有配置

## 🔍 故障排除

### 常见问题

#### Q: 修改配置后没有生效？
A: 调用 `reload_configs()` 或重启应用

#### Q: 验证脚本报错？
A: 检查配置格式，确保没有语法错误

#### Q: 模块配置没有继承主配置？
A: 确保使用 `load_config_with_base()` 或兼容层函数

#### Q: % 符号导致错误？
A: 使用 `%%` 转义，或使用 `interpolation=None`

### 调试工具
```python
# 查看合并后的配置
from src.config.utils import get_merged_config
config = get_merged_config("market.ini")
print(config)
```

## 📈 高级功能

### 环境特定配置
```bash
# 开发环境
cp config/app.ini config/app-dev.ini

# 生产环境  
cp config/app.ini config/app-prod.ini
```

### 配置版本控制
```python
import hashlib

def get_config_hash():
    """获取配置哈希，用于版本控制"""
    with open("config/app.ini", "rb") as f:
        return hashlib.md5(f.read()).hexdigest()
```

## 📞 支持

如有问题，请：
1. 运行 `python3 validate_config.py` 验证配置
2. 检查配置文件格式
3. 查看此文档的故障排除部分
4. 联系开发团队