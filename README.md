# MT5Services

Config rules:
- Runtime code should read config through `src.config`.
- `config/indicators.json` is the only indicator config entrypoint.
- `load_*`, `compat`, `fallback`, and watcher utilities are compatibility layers, not the primary runtime path.

MT5Services 是一个基于 FastAPI 的统一运行服务，围绕 MetaTrader 5 提供行情采集、历史落库、指标计算、账户与交易接口、经济日历风控和系统监控。

当前仓库已经收敛为单一运行模式，默认入口是 `python app.py`，不再区分多套启动方式。

## 功能概览

- 市场数据：`quote`、`ticks`、`ohlc`、盘中 OHLC 序列、SSE 流订阅
- 持久化：Ticks、Quotes、OHLC、指标结果、经济日历事件、运行任务状态
- 指标系统：`config/indicators.json` + `src/indicators/manager.py`
- 交易能力：账户查询、持仓/挂单查询、下单、平仓、改单、保证金预估
- 宏观风控：经济日历抓取、事件筛选、风险时间窗、交易前检查
- 系统监控：健康检查、队列状态、性能指标、启动阶段、有效配置快照

## 运行架构

统一启动链路如下：

1. `app.py` 解析 Host/Port 并启动 `uvicorn src.api:app`
2. `src/api/__init__.py` 创建 FastAPI 应用并注册全部路由
3. `src/api/deps.py` 在 `lifespan` 中初始化并启动核心组件

默认启动的核心组件：

- `MarketDataService`
- `StorageWriter`
- `BackgroundIngestor`
- `EconomicCalendarService`
- `PreTradeRiskService`
- `TradingService`
- `UnifiedIndicatorManager`
- `HealthMonitor`
- `MonitoringManager`

启动顺序：

1. `storage`
2. `ingestion`
3. `economic_calendar`
4. `indicators`
5. `monitoring`

运行时可通过 `GET /monitoring/startup` 和 `GET /monitoring/runtime-tasks` 查看阶段状态。

## 目录结构

```text
MT5Services/
├─ app.py
├─ config/
│  ├─ app.ini
│  ├─ market.ini
│  ├─ ingest.ini
│  ├─ storage.ini
│  ├─ economic.ini
│  ├─ mt5.ini
│  ├─ db.ini
│  ├─ cache.ini
│  └─ indicators.json
├─ src/
│  ├─ api/
│  ├─ clients/
│  ├─ config/
│  ├─ core/
│  ├─ indicators/
│  ├─ ingestion/
│  ├─ monitoring/
│  ├─ persistence/
│  └─ utils/
├─ tests/
│  ├─ api/
│  ├─ config/
│  ├─ core/
│  ├─ data/
│  ├─ indicators/
│  ├─ integration/
│  └─ smoke/
└─ examples/
```

## 安装

环境要求：

- Python 3.9+
- 已安装并可登录的 MetaTrader 5 终端
- PostgreSQL/TimescaleDB

推荐安装方式：

```bash
python -m venv .venv
```

```bash
# Linux/macOS
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

```bash
pip install -U pip
pip install -e .
```

开发依赖：

```bash
pip install -e ".[dev]"
```

测试依赖：

```bash
pip install -e ".[test]"
```

如果只使用 requirements：

```bash
pip install -r requirements.txt
```

## 最低配置要求

首次启动至少需要检查这些文件：

- `config/mt5.ini`
- `config/db.ini`
- `config/app.ini`
- `config/market.ini`

按需再配置：

- `config/economic.ini`
- `config/storage.ini`
- `config/indicators.json`

敏感信息建议放到本地私有覆盖文件，不要把真实密钥直接写入仓库配置：

- `config/*.local.ini`（例如 `config/market.local.ini`）
- `config/economic.local.ini`

## 启动

```bash
python app.py
```

或：

```bash
uvicorn src.api:app --host 0.0.0.0 --port 8808
```

默认可访问：

- [Swagger](http://localhost:8808/docs)
- [ReDoc](http://localhost:8808/redoc)
- [Health](http://localhost:8808/health)

## 核心接口

### 基础状态

- `GET /health`
- `GET /monitoring/health`
- `GET /monitoring/startup`
- `GET /monitoring/runtime-tasks`
- `GET /monitoring/config/effective`

### 市场数据

- `GET /symbols`
- `GET /symbol/info`
- `GET /quote`
- `GET /ticks`
- `GET /quotes/history`
- `GET /ohlc`
- `GET /ohlc/history`
- `GET /ohlc/intrabar/series`
- `GET /stream`

### 账户与交易

- `GET /account/info`
- `GET /account/positions`
- `GET /account/orders`
- `POST /trade/precheck`
- `POST /trade`
- `POST /close`
- `POST /close_all`
- `POST /cancel_orders`
- `POST /estimate_margin`
- `PUT /modify_orders`
- `PUT /modify_positions`
- `GET /positions`
- `GET /orders`

### 经济日历

- `POST /economic/calendar/refresh`
- `GET /economic/calendar`
- `GET /economic/calendar/upcoming`
- `GET /economic/calendar/high-impact`
- `GET /economic/calendar/curated`
- `GET /economic/calendar/risk-windows`
- `GET /economic/calendar/risk-windows/merged`
- `GET /economic/calendar/status`
- `GET /economic/calendar/trade-guard`
- `GET /economic/calendar/updates`

### 指标系统

- `GET /indicators/list`
- `GET /indicators/{symbol}/{timeframe}`
- `GET /indicators/{symbol}/{timeframe}/{indicator_name}`
- `POST /indicators/compute`
- `GET /indicators/performance/stats`
- `POST /indicators/cache/clear`
- `GET /indicators/dependency/graph`

## 配置文件职责

| 文件 | 作用 |
| --- | --- |
| `config/app.ini` | 共享交易范围、频率、限制、系统参数 |
| `config/market.ini` | API Host/Port、认证、日志 |
| `config/ingest.ini` | 采集节奏、性能、健康阈值 |
| `config/storage.ini` | 存储通道队列与 flush 策略 |
| `config/economic.ini` | 日历抓取、事件筛选、交易风控 |
| `config/mt5.ini` | MT5 连接参数 |
| `config/db.ini` | 数据库连接 |
| `config/cache.ini` | 缓存兼容参数 |
| `config/indicators.json` | 当前统一指标配置 |

更细的说明见 [CONFIG_GUIDE.md](CONFIG_GUIDE.md)。

## 测试与质量工具

当前 `tests/` 已按目录拆分：

- `tests/api`
- `tests/config`
- `tests/core`
- `tests/data`
- `tests/indicators`
- `tests/integration`
- `tests/smoke`

常用命令：

```bash
pytest
```

```bash
black src tests
isort src tests
mypy src
flake8 src tests
```

## API Key 认证

在 `config/market.ini` 中启用：

```ini
[security]
auth_enabled = true
api_key_header = X-API-Key
api_key =
```

更推荐通过本地私有覆盖文件提供：

```ini
[security]
api_key = replace-with-your-key
```

调用示例：

```bash
curl -H "X-API-Key: replace-with-your-key" http://localhost:8808/health
```

## 推荐阅读

- [QUICK_START.md](QUICK_START.md)
- [CONFIG_GUIDE.md](CONFIG_GUIDE.md)

如果文档与当前源码实现不一致，以 `app.py`、`src/api/`、`src/config/`、`src/indicators/manager.py` 为准。
