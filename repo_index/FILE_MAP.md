# MT5Services 文件映射文档

## 文件分类索引

### A. 应用入口和启动文件
| 文件路径 | 用途 | 重要性 | 最后修改 |
|----------|------|--------|----------|
| `app.py` | 统一应用启动脚本 | 高 | 2026-03-10 |
| `src/api/__init__.py` | 统一FastAPI应用入口 | 高 | 2026-03-10 |

### B. 配置文件
| 文件路径 | 用途 | 重要性 | 最后修改 |
|----------|------|--------|----------|
| `config/app.ini` | 主配置文件（单一信号源） | 高 | 2026-03-10 |
| `config/market.ini` | 市场数据配置 | 高 | 2026-03-10 |
| `config/ingest.ini` | 数据采集配置 | 高 | 2026-03-10 |
| `config/indicators.ini` | 指标计算配置 | 高 | 2026-03-10 |
| `config/db.ini` | 数据库配置 | 高 | 2026-03-10 |
| `config/cache.ini` | 缓存配置 | 中 | 2026-03-10 |

### C. API层文件
| 文件路径 | 用途 | 重要性 | 最后修改 |
|----------|------|--------|----------|
| `src/api/__init__.py` | API模块初始化 | 低 | 2026-03-10 |
| `src/api/market.py` | 市场数据API路由 | 高 | 2026-03-10 |
| `src/api/account.py` | 账户API路由 | 中 | 2026-03-10 |
| `src/api/trade.py` | 交易API路由 | 中 | 2026-03-10 |
| `src/api/monitoring.py` | 监控API路由（新增） | 中 | 2026-03-10 |
| `src/api/deps.py` | 原始依赖注入 | 高 | 2026-03-10 |
| `src/api/deps.py` | 增强版依赖注入 | 高 | 2026-03-10 |
| `src/api/schemas.py` | API数据模型定义 | 高 | 2026-03-10 |
| `src/api/error_codes.py` | 错误代码定义 | 中 | 2026-03-10 |

### D. 配置管理文件
| 文件路径 | 用途 | 重要性 | 最后修改 |
|----------|------|--------|----------|
| `src/config/__init__.py` | 配置模块导出 | 中 | 2026-03-10 |
| `src/config/compat.py` | 向后兼容配置接口 | 高 | 2026-03-10 |
| `src/config/centralized.py` | 集中式配置管理 | 高 | 2026-03-10 |
| `src/config/advanced_manager.py` | 高级配置管理器（新增） | 高 | 2026-03-10 |
| `src/config/utils.py` | 配置工具函数 | 中 | 2026-03-10 |

### E. 核心业务文件
| 文件路径 | 用途 | 重要性 | 最后修改 |
|----------|------|--------|----------|
| `src/core/__init__.py` | 核心模块初始化 | 低 | 2026-03-10 |
| `src/core/market_service.py` | 市场数据服务（内存缓存） | 高 | 2026-03-10 |
| `src/core/account_service.py` | 账户服务 | 中 | 2026-03-10 |
| `src/core/trading_service.py` | 交易服务 | 中 | 2026-03-10 |

### F. 指标计算文件
#### F1. 基础指标函数
| 文件路径 | 用途 | 重要性 | 最后修改 |
|----------|------|--------|----------|
| `src/indicators/__init__.py` | 指标模块导出 | 中 | 2026-03-10 |
| `src/indicators/base.py` | 指标基础工具函数 | 高 | 2026-03-10 |
| `src/indicators/mean.py` | 均值类指标（SMA, EMA, WMA） | 高 | 2026-03-10 |
| `src/indicators/momentum.py` | 动量类指标（RSI, MACD, ROC, CCI） | 高 | 2026-03-10 |
| `src/indicators/volatility.py` | 波动率指标（Bollinger, ATR, Keltner, Donchian） | 高 | 2026-03-10 |
| `src/indicators/volume.py` | 成交量指标（OBV, VWAP） | 中 | 2026-03-10 |
| `src/indicators/types.py` | 指标类型定义 | 中 | 2026-03-10 |
| `src/indicators/worker.py` | 原始指标计算工作器 | 高 | 2026-03-10 |

#### F2. 优化指标计算（新增）
| 文件路径 | 用途 | 重要性 | 最后修改 |
|----------|------|--------|----------|
| `src/indicators/optimized/worker_enhanced.py` | 增强指标计算器 | 高 | 2026-03-10 |
| `src/indicators/engine/pipeline_engine.py` | 指标流水线引擎 | 高 | 2026-03-10 |
| `src/indicators/engine/integration.py` | 指标集成器 | 高 | 2026-03-10 |

### G. 数据采集文件
| 文件路径 | 用途 | 重要性 | 最后修改 |
|----------|------|--------|----------|
| `src/ingestion/__init__.py` | 采集模块初始化 | 低 | 2026-03-10 |
| `src/ingestion/ingestor.py` | 后台数据采集器 | 高 | 2026-03-10 |

### H. 监控系统文件（新增）
| 文件路径 | 用途 | 重要性 | 最后修改 |
|----------|------|--------|----------|
| `src/monitoring/__init__.py` | 监控模块初始化 | 低 | 2026-03-10 |
| `src/monitoring/health_check.py` | 健康监控系统 | 高 | 2026-03-10 |

### I. 数据持久化文件
| 文件路径 | 用途 | 重要性 | 最后修改 |
|----------|------|--------|----------|
| `src/persistence/__init__.py` | 持久化模块初始化 | 低 | 2026-03-10 |
| `src/persistence/db.py` | TimescaleDB写入器 | 高 | 2026-03-10 |
| `src/persistence/storage_writer.py` | 通用存储写入器 | 中 | 2026-03-10 |

### J. 工具函数文件
| 文件路径 | 用途 | 重要性 | 最后修改 |
|----------|------|--------|----------|
| `src/utils/__init__.py` | 工具模块初始化 | 低 | 2026-03-10 |
| `src/utils/common.py` | 通用工具函数 | 中 | 2026-03-10 |
| `src/utils/event_store.py` | 事件存储系统（新增） | 高 | 2026-03-10 |
| `src/utils/memory_manager.py` | 智能内存管理器（新增） | 高 | 2026-03-10 |

### K. 客户端库文件
| 文件路径 | 用途 | 重要性 | 最后修改 |
|----------|------|--------|----------|
| `src/clients/__init__.py` | 客户端模块初始化 | 低 | 2026-03-10 |
| `src/clients/mt5_market.py` | MT5客户端库 | 高 | 2026-03-10 |

### L. 脚本文件
| 文件路径 | 用途 | 重要性 | 最后修改 |
|----------|------|--------|----------|
| `scripts/validate_config.py` | 配置验证脚本 | 中 | 2026-03-10 |

### M. 测试文件
| 文件路径 | 用途 | 重要性 | 最后修改 |
|----------|------|--------|----------|
| `test_enhancements.py` | 优化功能测试脚本（新增） | 中 | 2026-03-10 |
| `test_indicator_usage.py` | 指标计算使用测试（新增） | 中 | 2026-03-10 |

### N. 文档文件
| 文件路径 | 用途 | 重要性 | 最后修改 |
|----------|------|--------|----------|
| `ENHANCEMENTS_README.md` | 优化功能说明文档（新增） | 高 | 2026-03-10 |
| `repo_index/ARCHITECTURE.md` | 系统架构文档（新增） | 高 | 2026-03-10 |
| `repo_index/PROJECT_INDEX.md` | 项目索引文档（新增） | 高 | 2026-03-10 |
| `repo_index/FILE_MAP.md` | 文件映射文档（本文件） | 中 | 2026-03-10 |
| `repo_index/DEPENDENCIES.md` | 依赖文档（新增） | 中 | 2026-03-10 |
| `repo_index/LAST_ANALYSIS.json` | 最后分析记录（新增） | 低 | 2026-03-10 |

### O. 其他文件
| 文件路径 | 用途 | 重要性 | 最后修改 |
|----------|------|--------|----------|
| `requirements.txt` | Python依赖列表 | 高 | 2026-03-10 |

## 文件依赖关系图

### 核心依赖
```
app.py
├── src.api
│   ├── src.api.deps
│   │   ├── src.core.market_service
│   │   ├── src.ingestion.ingestor
│   │   ├── src.indicators.worker
│   │   │   ├── src.config.*
│   │   │   ├── src.indicators.mean
│   │   │   ├── src.indicators.momentum
│   │   │   ├── src.indicators.volatility
│   │   │   └── src.indicators.volume
│   │   └── src.persistence.*
│   └── src.api.market/account/trade
└── src.clients.mt5_market
```

### 优化模块依赖
```
__init__.py
├── src.api
│   ├── src.api.deps
│   │   ├── src.config.advanced_manager
│   │   ├── src.utils.memory_manager
│   │   ├── src.monitoring.health_check
│   │   ├── src.indicators.engine.integration
│   │   │   └── src.indicators.engine.pipeline_engine
│   │   └── src.utils.event_store
│   └── src.api.monitoring
└── app.py
```

## 关键文件说明

### 1. `src/core/market_service.py`
**作用**: 内存市场数据缓存，系统的核心数据枢纽
**依赖**: `src.clients.mt5_market`, `src.config`
**被依赖**: `src.api.*`, `src.ingestion.ingestor`, `src.indicators.worker`

### 2. `src/indicators/worker.py`
**作用**: 指标计算工作器，负责计算技术指标
**依赖**: `src.core.market_service`, `src.indicators.*`, `src.config`
**被依赖**: `src.api.deps`

### 3. `src/config/advanced_manager.py`
**作用**: 高级配置管理，提供热加载和类型安全
**依赖**: 无外部依赖（仅标准库）
**被依赖**: `src.api.deps`

### 4. `src/utils/event_store.py`
**作用**: SQLite事件存储，确保事件不丢失
**依赖**: SQLite3（Python内置）
**被依赖**: `src.api.deps`

### 5. `src/monitoring/health_check.py`
**作用**: 系统健康监控和告警
**依赖**: SQLite3（Python内置）
**被依赖**: `src.api.deps`, `src.api.monitoring`

## 新增文件说明（中期优化）

### 优化配置管理
- `src/config/advanced_manager.py`: 配置热加载、类型验证、环境变量覆盖

### 事件和内存管理
- `src/utils/event_store.py`: 持久化事件存储，替代内存队列
- `src/utils/memory_manager.py`: 智能内存监控和清理

### 指标计算优化
- `src/indicators/optimized/worker_enhanced.py`: 增强指标计算器（缓存一致性检查）
- `src/indicators/engine/pipeline_engine.py`: 指标流水线引擎（并行计算）
- `src/indicators/engine/integration.py`: 指标集成器（向后兼容）

### 监控系统
- `src/monitoring/health_check.py`: 健康监控系统
- `src/api/monitoring.py`: 监控API端点

### 应用入口优化
- `src/api/__init__.py`: 增强版应用入口
- `src/api/deps.py`: 增强版依赖注入
- `app.py`: 增强版启动脚本

### 文档和测试
- `ENHANCEMENTS_README.md`: 优化功能说明
- `test_enhancements.py`: 优化功能测试
- `repo_index/`: 项目知识缓存目录

## 文件修改历史

### 2026-03-10 重大更新（中期优化）
1. **新增优化模块**: 配置管理、事件存储、内存管理、监控系统
2. **新增指标引擎**: 流水线计算引擎，支持并行计算
3. **新增监控API**: 系统健康状态监控端点
4. **更新文档**: 完整的架构和项目文档
5. **修复问题**: 修复worker.py的BOM字符问题

### 关键修改文件
- `src/indicators/worker.py`: 移除UTF-8 BOM字符
- 所有新增文件: 见上述"新增文件说明"

## 文件维护指南

### 需要定期检查的文件
1. **配置文件**: `config/*.ini` - 检查配置一致性
2. **依赖文件**: `requirements.txt` - 更新依赖版本
3. **核心业务文件**: `src/core/*.py` - 检查业务逻辑
4. **监控文件**: `src/monitoring/*.py` - 检查监控规则

### 需要备份的文件
1. **配置文件**: `config/` 目录全部文件
2. **数据库配置**: `config/db.ini`（包含数据库连接信息）
3. **指标配置**: `config/indicators.ini`（交易策略依赖）

### 测试关键文件
运行以下测试确保系统正常：
```bash
# 测试优化功能
python test_enhancements.py

# 测试指标计算
python test_indicator_usage.py

# 验证配置
python scripts/validate_config.py
```

---

**文档最后更新**: 2026-03-10  
**文件总数**: 54个Python文件 + 配置文件 + 文档文件  
**总代码行数**: 约9000行  
**维护建议**: 每次重大修改后更新此文档

