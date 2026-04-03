# MT5Services

基于 FastAPI 的生产级量化交易平台，通过 MetaTrader 5 Python 绑定提供实时行情采集、技术指标计算、多策略信号生成、风险管理和自动交易执行。

## 技术栈

- **Python** 3.9–3.12
- **框架**: FastAPI + uvicorn
- **数据库**: TimescaleDB (PostgreSQL 时序扩展)
- **交易终端**: MetaTrader 5 (Windows Python binding)
- **后台任务**: threading (约 16-20 个活跃线程)

## 快速开始

### 1. 安装

```bash
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\Activate.ps1  # Windows PowerShell

pip install -U pip
pip install -e .
```

开发/测试依赖：

```bash
pip install -e ".[dev,test]"
```

### 2. 最小配置

首次启动需检查以下 4 个文件：

| 文件 | 用途 |
|------|------|
| `config/mt5.ini` | MT5 终端路径、账户 (login/password/server) |
| `config/db.ini` | PostgreSQL/TimescaleDB 连接参数 |
| `config/app.ini` | 交易品种、时间框架、采集间隔 |
| `config/market.ini` | API host/port、认证 (API Key)、CORS |

敏感信息放入 `config/*.local.ini`（已被 `.gitignore`）。

### 3. 启动

```bash
python app.py
# 或
uvicorn src.api:app --host 0.0.0.0 --port 8808
```

启动后访问：

- Swagger UI: `http://localhost:8808/docs`
- ReDoc: `http://localhost:8808/redoc`
- Health: `http://localhost:8808/health`
- 交易状态: `http://localhost:8808/v1/trade/state`
- 风险收口状态: `http://localhost:8808/v1/trade/state/closeout`
- 交易链路 Trace: `http://localhost:8808/v1/trade/trace/{signal_id}`

### 4. 启动验证

```bash
# 基础状态
curl http://localhost:8808/health
curl http://localhost:8808/monitoring/startup

# 行情数据
curl "http://localhost:8808/quote?symbol=XAUUSD"
curl "http://localhost:8808/ohlc?symbol=XAUUSD&timeframe=M1&limit=20"

# 指标
curl "http://localhost:8808/indicators/list"

# 信号
curl "http://localhost:8808/signals/strategies"

# 监控
curl "http://localhost:8808/monitoring/config/effective"
```

## 目录结构

```
MT5Services/
├── app.py                    # 启动入口
├── config/                   # 所有配置文件
│   ├── app.ini               # 品种/时间框架/采集间隔（全局单一来源）
│   ├── market.ini            # API 服务配置
│   ├── mt5.ini               # MT5 终端连接
│   ├── db.ini                # 数据库连接
│   ├── signal.ini            # 信号模块配置
│   ├── risk.ini              # 风险限制
│   ├── economic.ini          # 经济日历与 Trade Guard
│   ├── ingest.ini            # 后台采集配置
│   ├── storage.ini           # 持久化队列配置
│   ├── cache.ini             # 内存缓存覆盖
│   ├── indicators.json       # 指标定义与计算流水线
│   └── composites.json       # 复合策略组合定义
├── src/
│   ├── app_runtime/          # 应用运行时 (container/builder/runtime)
│   ├── api/                  # FastAPI 路由、中间件、Schema、DI 适配层
│   ├── calendar/             # 经济日历服务
│   ├── clients/              # MT5 客户端封装
│   ├── config/               # 配置加载、合并、Pydantic 模型
│   ├── indicators/           # 统一指标系统
│   ├── ingestion/            # 后台数据采集
│   ├── market/               # MarketDataService (内存缓存)
│   ├── market_structure/     # 市场结构分析
│   ├── monitoring/           # 健康检查
│   ├── persistence/          # TimescaleDB 写入器
│   ├── risk/                 # 风险规则与服务
│   ├── signals/              # 信号生成 (策略/运行时/投票/过滤)
│   ├── trading/              # 交易执行与持仓管理
│   ├── backtesting/          # 回测与参数优化
│   └── utils/                # 通用工具
├── tests/                    # 测试套件
└── docs/                     # 技术文档
```

## 核心数据流

```
MT5 Terminal
    ↓
BackgroundIngestor ─→ MarketDataService (内存缓存)
    │                       ↓
    └→ StorageWriter ─→ TimescaleDB

OHLC 收盘事件 → UnifiedIndicatorManager → 指标快照
    ↓
SignalRuntime (双队列: confirmed 优先, intrabar best-effort)
    ├─ Regime 检测 → 亲和度修正
    ├─ 策略评估 → 置信度管线
    ├─ VotingEngine (跨策略投票)
    └─ TradeExecutor → PendingEntryManager → MT5 下单
```

## 风险收口

交易侧的风险收口统一通过 `ExposureCloseoutController` 执行，不再由 API、EOD、仓位管理分别拼接：

- `ExposureCloseoutService`：负责一次性平掉全部持仓并撤销全部挂单
- `ExposureCloseoutController`：负责统一命令入口与最近一次收口状态
- `PositionManager`：在 EOD 时触发收口，但不再承担撤挂单编排

当前提供两个只读/命令入口：

- `GET /v1/trade/state/closeout`：查看最近一次风险收口状态
- `POST /v1/trade/closeout-exposure`：人工触发统一风险收口

## 配置系统

### 优先级（高→低）

```
config/*.local.ini  → config/*.ini  → 代码默认值 (Pydantic 模型)
```

### 关键规则

- `config/app.ini` 是品种/时间框架的**唯一来源**，禁止在其他文件中重复定义
- 通过 `from src.config import get_*_config` 访问配置，不直接解析 `.ini`
- `.env` 文件已废弃，所有配置在 `.ini` 中

## API 概览

Base URL: `http://<host>:8808` | 认证: `X-API-Key` 请求头

所有业务路由同时挂载在 `/v1/` 前缀和根路径下。新客户端应使用 `/v1/` 前缀。

| 路由前缀 | 功能 |
|---------|------|
| `/` | 行情 (symbols, quote, ticks, ohlc, stream SSE) |
| `/trade` | 交易 (下单, 平仓, 预检, 保证金估算) |
| `/account` | 账户 (info, positions, orders) |
| `/indicators` | 指标 (list, 查询, 按需计算) |
| `/signals` | 信号 (策略列表, 评估, 诊断, 投票, 质量监控) |
| `/economic` | 经济日历 (事件, 风险窗口, Trade Guard) |
| `/monitoring` | 系统监控 (health, startup, queues, config) |
| `/backtest` | 回测 (run, optimize, walk-forward, 参数推荐) |

响应统一包装：`ApiResponse[T]` — `{success, data, error, error_code, metadata}`

## 信号系统

30+ 内置策略，分为 6 类：

| 类型 | 策略数 | Scope | 代表 |
|------|--------|-------|------|
| 跨 TF 联动 | 7 | confirmed / intrabar | htf_trend_pullback, dual_tf_momentum, m5_scalp_rsi |
| 趋势跟踪 | 6 | confirmed | supertrend, roc_momentum, fib_pullback |
| 均值回归 | 4 | intrabar + confirmed | rsi_reversion, stoch_rsi, cci_reversion |
| 突破/波动率 | 6 | 混合 | donchian_breakout, keltner_bb_squeeze |
| 价格行为 | 2 | confirmed | price_action_reversal, order_block_entry |
| M5 快速标量 | 3 | intrabar + confirmed | m5_scalp_rsi, m5_momentum_burst |

另有 5 个复合策略和 4 组方向一致性投票（momentum/breakout/reversion/reversal）。

## 风险管理

交易请求经过多层校验（任一失败即拒绝）：

1. **信号过滤** — 时段/点差/经济事件/波动率异常
2. **Regime 亲和度** — 低亲和度策略直接跳过
3. **置信度阈值** — `< 0.55` 静默丢弃
4. **ExecutionGate** — 投票组/白名单/armed 准入
5. **PendingEntry** — 价格确认入场 (ATR 区间)
6. **PreTradeRiskService** — 日损失限制/保证金/频率
7. **TradeExecutor 安全** — 熔断器/持仓预检/成本检查
8. **PositionManager** — 日终自动平仓

### 交易应用边界

- `TradingCommandService`：只负责命令类动作，例如下单、预检、平仓、撤单、改单、交易控制更新。
- `TradingQueryService`：只负责查询类动作，例如账户信息、持仓/挂单查询、交易汇总、审计读取、健康状态。
- `dispatch_operation(...)`：仅接受命令操作，不再承载 `daily_summary`、`entry_status`、`positions`、`orders` 等读操作。
- API 与读模型默认依赖命令/查询服务，而不是直接依赖 `TradingModule` 的大而全入口。
- `TradeControlStateService`：负责 `auto_entry_enabled / close_only_mode` 状态与准入拦截。
- `TradeCommandAuditService`：负责交易命令审计读写与审计回放查询，只记录命令，不再混入查询调用。
- `TradeDailyStatsService`：负责日内交易统计聚合。
- `TradingModule`：保留为应用协调器，不再直接持有上述共享状态实现细节。

### 审计与 Trace

- `trade_command_audits`：交易命令审计表，只记录 `execute_trade / precheck_trade / close / cancel / modify` 等命令类动作。
- 查询类调用不再写入同一张审计表，避免把命令审计、健康探针和运行查询混成一个事实源。
- `GET /v1/trade/command-audits`：读取最近交易命令审计。
- `pipeline_trace_events`：PipelineEventBus 的持久化事实表，记录 `bar_closed / indicator_computed / snapshot_published / signal_filter_decided / signal_evaluated` 上游链路节点。
- `GET /v1/trade/trace/{signal_id}`：按 `signal_id` 聚合 `pipeline_trace_events / signal_events / auto_executions / trade_command_audits / pending_order_states / position_runtime_states / trade_outcomes`，输出从市场数据到交易结果的时间线与节点关系，供可视化与问题定位使用。
- `GET /v1/trade/trace/by-trace/{trace_id}`：按 pipeline `trace_id` 聚合同一条链路，覆盖“被过滤、尚未生成 signal_id”的排查场景。

模块布局规范见：[docs/architecture/module-layout.md](/D:/MT5Services/docs/architecture/module-layout.md)。

## 测试与质量

```bash
# 测试
pytest                       # 全部
pytest -m unit               # 仅单元测试
pytest -m "not slow"         # 跳过慢速测试

# 代码质量
black src/ tests/
isort src/ tests/
mypy src/
flake8 src/ tests/
```

## 文档

| 文档 | 说明 |
|------|------|
| [架构概览](docs/architecture/overview.md) | 系统架构、启动流程、数据流详解 |
| [数据流全链路](docs/architecture/data-flow.md) | 完整数据流转图与事件链 |
| [信号系统设计](docs/architecture/signal-system.md) | 策略开发、Regime、状态机、投票引擎 |
| [模块边界](docs/architecture/module-boundaries.md) | 各模块职责与依赖矩阵 |
| [价格确认入场](docs/design/pending-entry.md) | PendingEntry 机制设计方案 |

## 许可

私有仓库，未公开授权。
