# MT5Services 项目索引

## 项目基本信息
- **项目名称**: MT5Services
- **项目类型**: 个人单服务器量化交易系统
- **主要功能**: MT5数据采集、指标计算、API服务
- **技术栈**: Python, FastAPI, TimescaleDB, SQLite
- **部署环境**: 单服务器部署

## 目录结构

```
MT5Services/
├── config/                          # 配置文件目录
│   ├── app.ini                     # 主配置文件（单一信号源）
│   ├── market.ini                  # 市场配置（继承app.ini）
│   ├── ingest.ini                  # 采集配置（继承app.ini）
│   ├── indicators.ini              # 指标配置文件
│   ├── db.ini                      # 数据库配置
│   └── cache.ini                   # 缓存配置
│
├── src/                            # 源代码目录
│   ├── api/                        # API层
│   │   ├── __init__.py
│   │   ├── app.py                  # 原始应用入口
│   │   ├── __init__.py         # 增强版应用入口
│   │   ├── market.py               # 市场数据API
│   │   ├── account.py              # 账户API
│   │   ├── trade.py                # 交易API
│   │   ├── monitoring.py           # 监控API（新增）
│   │   ├── deps.py                 # 依赖注入（原始）
│   │   ├── deps.py        # 依赖注入（增强版）
│   │   ├── schemas.py              # API数据模型
│   │   └── error_codes.py          # 错误代码定义
│   │
│   ├── clients/                    # 客户端库
│   │   ├── __init__.py
│   │   └── mt5_market.py           # MT5客户端
│   │
│   ├── config/                     # 配置管理系统
│   │   ├── __init__.py
│   │   ├── compat.py               # 向后兼容配置接口
│   │   ├── centralized.py          # 集中式配置管理
│   │   ├── advanced_manager.py     # 高级配置管理器（新增）
│   │   └── utils.py                # 配置工具函数
│   │
│   ├── core/                       # 核心业务逻辑
│   │   ├── __init__.py
│   │   ├── market_service.py       # 市场数据服务（内存缓存）
│   │   ├── account_service.py      # 账户服务
│   │   └── trading_service.py      # 交易服务
│   │
│   ├── indicators/                 # 指标计算系统
│   │   ├── __init__.py
│   │   ├── base.py                 # 指标基础函数
│   │   ├── mean.py                 # 均值类指标（SMA, EMA, WMA）
│   │   ├── momentum.py             # 动量类指标（RSI, MACD, ROC, CCI）
│   │   ├── volatility.py           # 波动率指标（Bollinger, ATR, Keltner, Donchian）
│   │   ├── volume.py               # 成交量指标（OBV, VWAP）
│   │   ├── types.py                # 指标类型定义
│   │   ├── worker.py               # 原始指标计算工作器
│   │   │
│   │   ├── optimized/              # 优化指标计算（新增）
│   │   │   └── worker_enhanced.py  # 增强指标计算器
│   │   │
│   │   └── engine/                 # 指标计算引擎（新增）
│   │       ├── __init__.py
│   │       ├── pipeline_engine.py  # 指标流水线引擎
│   │       └── integration.py      # 指标集成器
│   │
│   ├── ingestion/                  # 数据采集层
│   │   ├── __init__.py
│   │   └── ingestor.py             # 后台数据采集器
│   │
│   ├── monitoring/                 # 监控系统（新增）
│   │   ├── __init__.py
│   │   └── health_check.py         # 健康监控系统
│   │
│   ├── persistence/                # 数据持久化层
│   │   ├── __init__.py
│   │   ├── db.py                   # TimescaleDB写入器
│   │   └── storage_writer.py       # 通用存储写入器
│   │
│   └── utils/                      # 工具函数
│       ├── __init__.py
│       ├── common.py               # 通用工具函数
│       ├── event_store.py          # 事件存储系统（新增）
│       └── memory_manager.py       # 智能内存管理器（新增）
│
├── repo_index/                     # 项目知识缓存（新增）
│   ├── ARCHITECTURE.md            # 系统架构文档
│   ├── PROJECT_INDEX.md           # 项目索引文档（本文件）
│   ├── FILE_MAP.md                # 文件映射文档
│   ├── DEPENDENCIES.md            # 依赖文档
│   └── LAST_ANALYSIS.json         # 最后分析记录
│
├── scripts/                       # 脚本目录
│   └── validate_config.py         # 配置验证脚本
│
├── tests/                         # 测试目录
│   └── integration/               # 集成测试
│
├── app.py                # 增强版服务启动脚本（新增）
├── test_enhancements.py           # 优化功能测试脚本（新增）
├── ENHANCEMENTS_README.md         # 优化功能说明文档（新增）
├── app.py                         # 原始应用启动脚本
└── requirements.txt               # Python依赖列表
```

## 关键文件说明

### 1. 应用入口文件
- **`app.py`**: 原始FastAPI应用入口
- **`__init__.py`**: 增强版应用入口（包含监控和优化功能）
- **`app.py`**: 增强版服务启动脚本

### 2. 配置文件
- **`config/app.ini`**: 主配置文件，单一信号源
- **`config/indicators.ini`**: 指标计算配置，定义计算哪些指标

### 3. 核心业务文件
- **`src/core/market_service.py`**: 内存市场数据缓存服务
- **`src/ingestion/ingestor.py`**: 后台数据采集器
- **`src/indicators/worker.py`**: 原始指标计算工作器

### 4. 新增优化文件
- **`src/config/advanced_manager.py`**: 高级配置管理器（热加载、验证）
- **`src/utils/event_store.py`**: 事件存储系统（SQLite持久化）
- **`src/utils/memory_manager.py`**: 智能内存管理器
- **`src/monitoring/health_check.py`**: 健康监控系统
- **`src/indicators/engine/pipeline_engine.py`**: 指标流水线引擎
- **`src/api/monitoring.py`**: 监控API端点

### 5. 文档文件
- **`repo_index/ARCHITECTURE.md`**: 系统架构文档
- **`ENHANCEMENTS_README.md`**: 优化功能使用说明

## 模块依赖关系

### 核心依赖链
```
app.py/__init__.py
    ├── src.api.deps
    │   ├── src.core.market_service
    │   ├── src.ingestion.ingestor
    │   └── src.indicators.worker/worker_enhanced
    │       ├── src.config.*
    │       ├── src.indicators.* (指标函数)
    │       └── src.persistence.*
    └── src.api.* (路由模块)
```

### 优化模块依赖
```
优化模块依赖关系:
__init__.py
    ├── src.config.advanced_manager
    ├── src.utils.memory_manager
    ├── src.monitoring.health_check
    ├── src.indicators.engine.integration
    │   └── src.indicators.engine.pipeline_engine
    └── src.utils.event_store
```

## 启动流程

### 原始版本启动
```bash
# 方式1: 直接运行
python app.py

# 方式2: 使用uvicorn
python -m uvicorn src.api:app --host 0.0.0.0 --port 8810
```

### 增强版本启动
```bash
# 使用增强版启动脚本
python app.py

# 或者直接运行增强版应用
python -m uvicorn src.api:app --host 0.0.0.0 --port 8810
```

## 配置加载流程

### 原始配置加载
```
应用启动 → 加载各模块配置 → 初始化服务 → 启动API
```

### 优化后配置加载
```
应用启动 → 高级配置管理器 → 热加载监听 → 类型验证 → 初始化服务 → 启动API
                     ↓
                 环境变量覆盖
```

## 数据流说明

### 市场数据流
```
MT5终端 → BackgroundIngestor → MarketDataService → API客户端
                              ↓
                          IndicatorWorker → TimescaleDB
```

### 优化后数据流
```
MT5终端 → BackgroundIngestor → MarketDataService → IndicatorPipelineEngine → TimescaleDB
                              ↓                      ↓
                          EventStore              HealthMonitor
                              ↓                      ↓
                          SQLite持久化            监控告警
```

## 监控端点

### 健康监控
- `GET /monitoring/health` - 系统健康状态
- `GET /monitoring/performance` - 性能指标
- `GET /monitoring/events` - 事件统计
- `GET /monitoring/consistency` - 缓存一致性检查

### 内存监控
- `GET /monitoring/memory/report` - 内存使用报告
- `GET /monitoring/memory/leaks` - 内存泄漏检测

### 指标监控
- `GET /monitoring/indicators/pipeline` - 指标流水线状态
- `GET /monitoring/indicators/performance` - 指标计算性能

## 测试文件

### 功能测试
- **`test_enhancements.py`**: 优化功能测试脚本
- **`test_indicator_usage.py`**: 指标计算使用测试

### 配置验证
- **`scripts/validate_config.py`**: 配置一致性验证

## 开发指南

### 添加新指标
1. 在`src/indicators/`相应模块中添加指标函数
2. 在`config/indicators.ini`中添加配置节
3. 指标函数签名: `fn(bars, params) -> dict`

### 添加新API端点
1. 在`src/api/`相应模块中添加路由
2. 在`src/api/schemas.py`中添加数据模型
3. 在`src/api/deps.py`或`deps.py`中添加依赖

### 使用优化功能
1. 导入相应的优化模块
2. 调用`get_*()`单例函数获取实例
3. 参考`ENHANCEMENTS_README.md`使用说明

## 维护说明

### 定期维护任务
1. **检查依赖更新**: 更新`requirements.txt`
2. **验证配置**: 运行`scripts/validate_config.py`
3. **测试优化功能**: 运行`test_enhancements.py`
4. **更新文档**: 更新`repo_index/`中的文档

### 故障排除
1. **查看日志**: 系统日志位于标准输出
2. **使用监控API**: 通过监控端点检查系统状态
3. **检查配置**: 验证配置文件是否正确
4. **测试连接**: 检查MT5连接和数据库连接

---

**最后更新**: 2026-03-10  
**维护者**: Hugo  
**项目状态**: 生产就绪（含中期优化）

