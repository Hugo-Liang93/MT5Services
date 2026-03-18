# CLAUDE.md — MT5Services Codebase Guide

This file is the primary reference for AI assistants working on MT5Services. It covers architecture, conventions, workflows, and rules to follow when making changes.

---

## Language Preference

**请始终使用中文回复。** All responses, explanations, code comments suggestions, and conversations must be in Chinese (Simplified). Code itself (variable names, function names, string literals in source files) follows the existing conventions of each file.

---

## Project Overview

**MT5Services** is a production-ready FastAPI trading platform that connects to MetaTrader 5 (MT5) terminals. It provides real-time market data, technical indicators, signal generation, risk management, and trade execution via REST API, backed by TimescaleDB (PostgreSQL with time-series hypertables).

- **Python**: 3.9–3.12
- **Framework**: FastAPI + uvicorn
- **Database**: TimescaleDB (PostgreSQL extension)
- **Trading terminal**: MetaTrader 5 (Windows-only Python binding)
- **Entry point**: `python app.py` or `uvicorn src.api:app`

---

## Repository Structure

```
MT5Services/
├── app.py                    # 主启动器（解析 host/port，启动 uvicorn）
├── pyproject.toml            # 项目元数据、依赖、工具配置
├── requirements.txt          # 运行时依赖
├── config/                   # 所有配置文件
│   ├── app.ini               # 单一信号源：品种、时间框架、采集间隔、缓存限制
│   ├── market.ini            # API 服务配置（host、port、认证、CORS）
│   ├── mt5.ini               # MT5 终端连接与账户配置
│   ├── db.ini                # TimescaleDB 连接配置
│   ├── ingest.ini            # 后台数据采集配置
│   ├── storage.ini           # 多通道队列持久化配置
│   ├── economic.ini          # 经济日历与 Trade Guard 配置
│   ├── risk.ini              # 风险限制（仓位数量、SL/TP 要求）
│   ├── cache.ini             # 运行时内存缓存大小（覆盖 app.ini [limits]）
│   ├── signal.ini            # 信号模块配置
│   └── indicators.json       # 指标定义与计算流水线
├── src/
│   ├── api/                  # FastAPI 路由、中间件、Schema、DI 容器
│   ├── clients/              # MT5 客户端封装（行情、交易、账户）
│   ├── config/               # 配置加载、合并、Pydantic 模型
│   ├── core/                 # 核心服务（行情缓存、经济日历、账户）
│   ├── indicators/           # 统一指标系统（管理器、引擎、缓存）
│   ├── ingestion/            # 后台 Tick/OHLC/Intrabar 数据采集
│   ├── persistence/          # TimescaleDB 写入器、队列持久化
│   ├── risk/                 # 风险规则、模型、服务
│   ├── signals/              # 信号生成策略、运行时、过滤器
│   ├── trading/              # TradingModule、账户注册、信号执行器
│   ├── monitoring/           # 健康检查
│   └── utils/                # 通用工具、事件存储、内存管理器
├── tests/                    # 测试套件（镜像 src/ 结构）
└── docs/                     # 代码评审记录、内部文档
```

---

## Configuration System

### Critical Rule: `config/app.ini` is the Single Source of Truth

所有交易品种、时间框架和全局采集间隔**仅**在 `config/app.ini` 中定义。其他 `.ini` 文件从此继承。禁止在其他配置文件中重复定义这些参数。

### Resolution Order (highest to lowest priority)

```
config/*.local.ini  （已被 .gitignore，用于密钥和机器特定覆盖）
    ↓
config/*.ini        （已提交的基础配置）
    ↓
code defaults       （src/config/centralized.py 中 Pydantic 模型的字段默认值）
```

### Creating Local Overrides

```bash
cp config/market.local.ini.example config/market.local.ini
# 编辑 config/market.local.ini，填入密钥和本地覆盖值
```

### No `.env` Files

`.env` 文件已**废弃**。所有配置均在 `.ini` 文件中。`python-dotenv` 仅作为遗留兼容依赖保留。

### Key Config Files

| 文件 | 用途 |
|------|------|
| `config/app.ini` | 交易品种、时间框架、采集间隔、缓存限制 |
| `config/market.ini` | API host/port、认证（API key）、CORS |
| `config/mt5.ini` | MT5 终端路径、账户配置（login/password/server） |
| `config/db.ini` | PostgreSQL/TimescaleDB 连接参数 |
| `config/ingest.ini` | 采集间隔、回填限制、重试配置 |
| `config/storage.ini` | 队列通道大小、刷写间隔、批处理大小、溢出策略 |
| `config/economic.ini` | 日历数据源（FRED、TradingEconomics）、Trade Guard 窗口 |
| `config/risk.ini` | 最大仓位数、SL/TP 要求 |
| `config/cache.ini` | 运行时内存缓存大小（覆盖 app.ini [limits]，优先级更高） |
| `config/signal.ini` | 自动交易、仓位大小、过滤条件、状态机参数 |
| `config/indicators.json` | 指标定义、参数、依赖关系、流水线配置 |

### Config Validation

配置验证在服务**启动时自动执行**（`src/config/utils.py` → `ConfigValidator`），无需手动运行验证脚本。

---

## Architecture

### Startup Sequence

组件按以下顺序初始化（见 `src/api/deps.py` lifespan）：

1. **Storage** — TimescaleDB 写入器 + 队列通道
2. **Ingestion** — 后台行情数据采集线程
3. **Economic Calendar Service** — 拉取并缓存经济日历事件
4. **Indicators** — 统一指标管理器
5. **Monitoring** — 健康检查
6. **Trading Module** — 账户注册、风险检查

关闭顺序相反。所有组件作为单例通过 FastAPI 的 `Depends()` 访问。

### Data Flow

```
MT5 Terminal
    ↓ （后台线程）
BackgroundIngestor (src/ingestion/ingestor.py)
    ↓                       ↓
MarketDataService       StorageWriter（多通道队列）
（内存缓存）                  ↓
    ↓                   TimescaleDB
API 路由从缓存读取
    ↓
OHLC 收盘事件 → IndicatorManager → 计算 → 持久化
              → SignalRuntime → 策略评估 → 执行交易
```

### In-Memory Cache Architecture

`MarketDataService` (`src/core/market_service.py`) 是**运行时数据的唯一权威**：
- 持有所有 Tick、报价、OHLC、Intrabar 状态
- 通过 `RLock` 保证线程安全
- 单一写入者：`BackgroundIngestor`
- 多个读取者：API 路由、指标管理器、信号运行时
- TimescaleDB 是**持久化备份**，不是主读路径

### Multi-Channel Persistence

`StorageWriter` (`src/persistence/storage_writer.py`) 每类数据使用独立的异步队列：

| 通道 | 队列大小 | 刷写间隔 | 批处理大小 | 满溢策略 |
|------|---------|---------|-----------|---------|
| ticks | 20,000 | 1s | 1,000 | drop_oldest |
| quotes | 5,000 | 1s | 200 | auto |
| intrabar | 10,000 | 5s | 200 | drop_newest |
| ohlc | 5,000 | 1s | 200 | block |
| ohlc_indicators | 5,000 | 1s | 200 | block |
| economic_calendar | 1,000 | 2s | 100 | auto |

### Signal Runtime Queue Architecture

`SignalRuntime` 使用**两个独立队列**按 scope 分离，防止高频 intrabar 事件挤占 confirmed（K 线收盘）事件：

| 队列 | 大小 | 可丢弃 | 说明 |
|------|------|--------|------|
| `_confirmed_events` | 512 | 否 | K 线收盘信号，不可丢失；优先消费 |
| `_intrabar_events` | 4,096 | 是 | 实时预览信号；仅在 confirmed 队列为空时处理 |

`process_next_event()` 总是先 `get_nowait()` 从 confirmed 队列取，空才 blocking wait intrabar 队列。

### Event-Driven Indicators & Signals

- OHLC bar 收盘 → `IndicatorManager` 计算该品种/时间框架的所有指标
- Intrabar 事件 → `SignalRuntime` 评估策略
- 指标计算支持三种模式：`standard`、`incremental`、`parallel`（在 `indicators.json` 中按指标配置）

---

## Key Source Files

| 文件 | 职责 |
|------|------|
| `src/api/deps.py` | DI 容器、启动/关闭生命周期、组件单例 |
| `src/api/__init__.py` | FastAPI app、CORS 中间件、API key 认证、路由注册 |
| `src/config/centralized.py` | Pydantic 配置模型（所有配置节） |
| `src/config/advanced_manager.py` | 配置加载器：合并 base + local .ini → Pydantic 模型 |
| `src/core/market_service.py` | 线程安全的内存行情缓存 |
| `src/ingestion/ingestor.py` | 后台数据采集（Tick、OHLC、Intrabar） |
| `src/indicators/manager.py` | 统一指标编排器 |
| `src/persistence/db.py` | TimescaleDB 写入操作、schema 初始化、hypertable |
| `src/persistence/storage_writer.py` | 基于队列的多通道持久化 |
| `src/trading/service.py` | TradingModule：账户、持仓、订单、交易生命周期 |
| `src/signals/runtime.py` | 事件驱动的信号评估与交易执行（双队列架构） |
| `src/core/economic_calendar_service.py` | 日历同步、风险窗口计算、Trade Guard |

---

## API Endpoints

Base URL: `http://<host>:8808`（默认）

Authentication: `X-API-Key` 请求头（在 `config/market.ini` 中配置）

| Router | Prefix | 主要端点 |
|--------|--------|---------|
| market | `/` | `/symbols`, `/quote`, `/ticks`, `/ohlc`, `/stream`（SSE） |
| trade | `/` | `/trade`, `/close`, `/close_all`, `/trade/precheck`, `/estimate_margin` |
| account | `/account` | `/account/info`, `/account/positions`, `/account/orders` |
| economic | `/economic` | `/economic/calendar`, `/economic/calendar/risk-windows`, `/economic/calendar/trade-guard` |
| indicators | `/indicators` | `/indicators/list`, `/indicators/{symbol}/{timeframe}`, `/indicators/compute` |
| monitoring | `/monitoring` | `/monitoring/health`, `/monitoring/startup`, `/monitoring/queues`, `/monitoring/config/effective` |
| signal | `/signal` | signal 端点 |

所有响应使用通用包装器：
```python
ApiResponse[T]:
    success: bool
    data: T | None
    error: str | None          # 人类可读的错误信息
    error_code: str | None     # AIErrorCode 枚举值
    metadata: dict | None
```

---

## Risk Management Layers

交易请求从内到外经过多层校验：

1. **Signal filters** (`src/signals/filters.py`)：经济事件过滤、价差过滤、交易时段过滤
2. **Pre-trade risk service** (`src/core/pretrade_risk_service.py`)：账户级别检查
3. **Risk rules** (`src/risk/rules.py`)：仓位限制、最大手数、SL/TP 要求
4. **Trade guard** (`src/core/economic_calendar_service.py`)：在经济事件窗口内阻止交易

---

## Account Model

- **每个服务实例绑定一个 MT5 账户**（在 `config/mt5.ini` 中选择）
- 支持多账户配置（`[account.demo]`、`[account.live]`），用于多实例部署
- `[mt5]` 节中的 `default_account` 选择启动时激活的账户
- 账户凭据存放在 `config/mt5.local.ini`（已被 .gitignore）
- ⚠️ 账户在运行时**不可**通过 API 切换，需重启服务

---

## Indicator System

指标在 `config/indicators.json` 中配置：

```json
{
  "name": "sma_20",
  "func_path": "src.indicators.core.mean.sma",
  "params": {"period": 20, "min_bars": 20},
  "dependencies": [],
  "compute_mode": "standard",
  "enabled": true,
  "tags": ["trend", "moving_average"]
}
```

可用的计算模式：
- `standard` — 每次 bar 收盘时全量重计算
- `incremental` — 仅追加更新（流式场景更快）
- `parallel` — 与其他指标并行计算

流水线（`src/indicators/engine/pipeline.py`）遵循依赖顺序，使用依赖图保证正确执行顺序。

---

## Development Workflows

### Installation

```bash
pip install -e ".[dev,test]"
```

### Running the Service

```bash
# 启动服务
python app.py

# 或直接用 uvicorn
uvicorn src.api:app --host 0.0.0.0 --port 8808 --reload
```

### Running Tests

```bash
# 全部测试
pytest

# 仅单元测试
pytest -m unit

# 仅集成测试
pytest -m integration

# 跳过慢速测试
pytest -m "not slow"

# 带覆盖率
pytest --cov=src --cov-report=html

# 指定模块
pytest tests/indicators/
```

### Code Quality

```bash
# 格式化代码
black src/ tests/

# 整理导入
isort src/ tests/

# 类型检查
mypy src/

# 代码检查
flake8 src/ tests/
```

### Code Style (enforced by tools)

| 工具 | 配置 | 行长度 |
|------|------|--------|
| black | `[tool.black]` in pyproject.toml | 88 |
| isort | profile = "black" | 88 |
| mypy | strict mode（见 pyproject.toml） | — |
| flake8 | `[tool.flake8]` | 88 |

---

## Code Conventions

### Type Safety

- **mypy strict mode** 强制执行，所有函数必须有类型注解。
- 不得在无充分理由的情况下使用 `Any`。
- 所有数据边界（API 请求/响应、配置）使用 Pydantic 模型。

Mypy 豁免（忽略缺失存根）：
- `pandas.*`, `numpy.*`, `sqlalchemy.*`

### Pydantic Version

使用 **Pydantic v2**（`>=2.6.0`），请用 v2 API：
- `model_validate()` 而非 `parse_obj()`
- `model_dump()` 而非 `dict()`
- `model_config = ConfigDict(...)` 而非 `class Config:`

### Error Handling

- 使用类型化错误码（`src/api/error_codes.py` 中的 `AIErrorCode` 枚举）
- 路由处理器始终返回 `ApiResponse[T]` 包装器
- 在错误响应中包含 `suggested_action`（需要时）
- 使用 `structlog` 结构化日志，不使用 `logging`

### Thread Safety

- `MarketDataService` 使用 `RLock`——读写均须持锁
- `StorageWriter` 队列线程安全——只 put 数据，不访问内部结构
- 不得在无锁的情况下跨线程共享可变状态

### Configuration Access

通过 `src/config/advanced_manager.py` 公开方法访问配置：
```python
from src.config import get_trading_config, get_api_config, get_db_config
# 不要直接解析 .ini 文件
```

### Dependency Injection

向 `src/api/deps.py` 添加新服务组件：
- 在 `AppDeps` 容器中注册
- 在 lifespan context manager 中添加启动/关闭逻辑
- 通过 `FastAPI.Depends()` 在路由处理器中暴露

### Adding New Indicators

1. 在 `src/indicators/core/` 中实现函数（参考 `mean.py`、`momentum.py` 等现有模式）
2. 在 `config/indicators.json` 中添加条目，填写 `func_path`、`params`、`dependencies`
3. 在 `tests/indicators/` 中添加测试

### Adding New Signal Strategies（SOP）

每个信号策略都是一个独立的「自描述单元」，必须完整声明四类属性才算合格。

#### 策略类必填属性清单

```python
class MyNewStrategy:
    # 1. 唯一名称（用于注册、日志、API）
    name = "my_new_strategy"

    # 2. 所需指标（对应 config/indicators.json 中的 name 字段）
    required_indicators = ("adx14", "boll20")

    # 3. 接收快照的 scope
    #    "confirmed" = 仅 bar 收盘（指标完整，适合趋势/突破策略）
    #    "intrabar"  = 盘中实时（适合均值回归类，需要捕捉极值的策略）
    preferred_scopes = ("confirmed",)

    # 4. ⚠️ Regime 亲和度（必填，不可省略）
    #    决定该策略在各种行情状态下的置信度乘数（0.0–1.0）
    #    乘数语义：
    #      1.00 = 该行情下完全可信，信号不衰减
    #      0.50 = 置信度减半（原 0.70 → 0.35，低于默认阈值 0.55，将被静默）
    #      0.10 = 几乎完全压制，仅极高置信度信号才能通过
    regime_affinity = {
        RegimeType.TRENDING:  0.XX,  # 趋势行情（ADX≥25）时的适配度，附说明
        RegimeType.RANGING:   0.XX,  # 震荡行情（ADX<20）时的适配度，附说明
        RegimeType.BREAKOUT:  0.XX,  # 突破/Squeeze 释放时的适配度，附说明
        RegimeType.UNCERTAIN: 0.XX,  # 过渡区（ADX 20-25）时的适配度
    }
```

#### Regime 亲和度设计指南

| 策略类型 | TRENDING | RANGING | BREAKOUT | UNCERTAIN |
|---------|---------|---------|---------|---------|
| 趋势跟踪（MA Cross、Supertrend、EMA Ribbon）| 1.00 | 0.1–0.3 | 0.4–0.6 | 0.5 |
| 均值回归（RSI、StochRSI）| 0.2–0.3 | 1.00 | 0.3–0.4 | 0.6 |
| 突破/波动率（Donchian、BB Squeeze、Keltner）| 0.3–0.9 | 0.15–0.55 | 1.00 | 0.45–0.65 |

**核心原则**：亲和度 × 原始 confidence < `min_preview_confidence`（默认 0.55）时，
SignalRuntime 状态机自然忽略该信号，无需在策略逻辑内做 Regime 判断。

#### 完整新增步骤

1. 在 `src/signals/strategies.py` 中实现策略类（包含上述四个属性 + `evaluate()` 方法）
2. 在 `src/signals/service.py` 的默认策略列表中注册实例
3. 在 `tests/signals/` 中添加单元测试，覆盖四种 Regime 下的输出
4. （可选）在 `config/signal.ini` 中调整 `min_preview_confidence` / `cooldown_seconds`

#### Regime 分类逻辑参考（`src/signals/regime.py`）

```
优先级：
  1. Keltner-Bollinger Squeeze（BB完全在KC内）→ BREAKOUT
  2. ADX ≥ 25 → TRENDING
  3. ADX < 20 且 BB宽度 < 0.5% → BREAKOUT（蓄力盘整）
  4. ADX < 20 → RANGING
  5. 20 ≤ ADX < 25 → UNCERTAIN
  6. 无指标数据 → UNCERTAIN（兜底）
```

### Adding New API Routes

1. 在 `src/api/` 中创建路由文件
2. 在 `src/api/schemas.py` 中添加 Pydantic 请求/响应 Schema
3. 在 `src/api/__init__.py` 中注册路由
4. 在 `src/api/error_codes.py` 中添加错误码

---

## Testing Conventions

### Test Structure

测试目录镜像 `src/` 结构：
```
tests/api/          → src/api/
tests/config/       → src/config/
tests/core/         → src/core/
tests/indicators/   → src/indicators/
tests/signals/      → src/signals/
tests/trading/      → src/trading/
tests/integration/  → 端到端流程
tests/smoke/        → 基础冒烟测试
```

### Test Markers

```python
@pytest.mark.unit
@pytest.mark.integration
@pytest.mark.slow
```

### Async Tests

```python
import pytest

@pytest.mark.asyncio
async def test_something():
    ...
```

### Fixtures and Factories

- 使用 `factory-boy` 创建模型工厂
- 使用 `faker` 生成真实测试数据
- 使用 `httpx.AsyncClient` 进行 API 集成测试

---

## Production Dependencies (Optional)

```bash
pip install -e ".[prod]"
```

新增：`gunicorn`, `uvloop`, `sentry-sdk`

---

## Known Issues & Notes

- **CORS**：通配符 origin（`*`）与 `allow_credentials=True` 不兼容（已在 `src/api/__init__.py` 修复）。若需认证 + 跨域，请在 `market.local.ini` 中配置具体的 allowed_origins。
- **MT5 Python 绑定**：仅支持 Windows。需要 MT5 的测试必须在 Windows 上运行或使用 Mock。
- **TimescaleDB**：首次启动前需确保 PostgreSQL 已安装 TimescaleDB 扩展，schema 在首次启动时自动初始化。
- **Config snapshot at import time**：`src/api/__init__.py` 在导入时一次性读取 API 配置，修改 `market.ini` 后需重启服务。
- **Health endpoint**：`/health` 将存活状态与所有服务耦合。Kubernetes 部署时建议分离 liveness（`/health/live`）和 readiness（`/health/ready`）。
- **`get_signal_config()` 已知 bug**：`src/config/centralized.py` 中调用了不存在的 `ConfigValidator.validate_model()`，直接调用此函数会抛出 `AttributeError`。信号配置目前通过兼容层加载，暂不受影响。

---

## File Naming Conventions

| 模式 | 规范 |
|------|------|
| 源码模块 | `snake_case.py` |
| 配置文件 | `module_name.ini` |
| 本地覆盖 | `module_name.local.ini`（已被 .gitignore） |
| 测试文件 | `test_<module>.py` |
| Schema 文件 | 以对应的数据库表名命名 |

---

## Git Workflow

- 默认分支：`main`
- 开发分支：`claude/<feature>-<id>` 或描述性功能分支
- 禁止提交密钥——凭据存放在 `*.local.ini`（已被 .gitignore）
- 提交代码变更前运行 `black`、`isort`、`mypy`、`flake8`
