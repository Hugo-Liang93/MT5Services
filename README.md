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

敏感信息（凭据/API key）和个人调优参数（策略权重/风险限制）放入 `config/*.local.ini`（已被 `.gitignore`）。提交到 Git 的 `.ini` 文件中对应字段已置空。

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
│   ├── signal.ini            # 信号模块配置（调优参数在 signal.local.ini）
│   ├── risk.ini              # 风险限制（具体阈值在 risk.local.ini）
│   ├── backtest.ini          # 回测与参数优化
│   ├── paper_trading.ini     # Paper Trading 影子交易
│   ├── economic.ini          # 经济日历与 Trade Guard
│   ├── ingest.ini            # 后台采集配置
│   ├── storage.ini           # 持久化队列配置
│   ├── cache.ini             # 内存缓存覆盖
│   └── indicators.json       # 指标定义与计算流水线
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
│   ├── monitoring/           # 健康监控（内存环形缓冲 + SQLite 告警）
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

人工/API 触发的收口在成功完成后，会根据 `config/app.ini` 中的
`[trading_ops].runtime_mode_after_manual_closeout` 自动切换运行模式。
当前默认值为 `ingest_only`。

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
| `/trade` | 交易 (下单, 平仓, 预检, 保证金估算, 链路 trace) |
| `/account` | 账户 (info, positions, orders) |
| `/indicators` | 指标 (list, 查询, 按需计算) |
| `/signals` | 信号 (策略列表, 评估, 诊断, 投票, 质量监控) |
| `/economic` | 经济日历 (事件, 风险窗口, Trade Guard) |
| `/monitoring` | 系统监控 (health, startup, queues, config) |
| `/decision` | 决策摘要 (上下文融合 brief) |
| `/backtest` | 回测 (run, optimize, walk-forward, 参数推荐) |
| `/paper-trading` | 模拟交易 (start/stop, trades, positions, metrics) |
| `/admin` | 管理后台 (dashboard, config, strategies, SSE) |
| `/studio` | Studio (16 agent 实时状态, events, SSE stream) |

响应统一包装：`ApiResponse[T]` — `{success, data, error, error_code, metadata}`

## 信号系统

当前默认策略体系是纯结构化策略，代码入口在 `src/signals/strategies/structured/`，注册目录在 `src/signals/strategies/catalog.py`。

| 策略实例 | 类别 | 默认 Scope | 说明 |
|----------|------|------------|------|
| `structured_trend_continuation` | `multi_tf` | confirmed | HTF 趋势方向 + LTF 回调 |
| `structured_trend_h4` | `multi_tf` | confirmed | H4 HTF 变体 |
| `structured_sweep_reversal` | `price_action` | confirmed | 扫流动性后反转 |
| `structured_breakout_follow` | `breakout` | confirmed | ADX/DI 与结构突破跟随 |
| `structured_range_reversion` | `reversion` | confirmed + intrabar | RSI/Bollinger 区间回归 |
| `structured_session_breakout` | `session` | confirmed | 亚盘区间后的时段突破 |
| `structured_trendline_touch` | `trend` | confirmed | 趋势线触碰与结构位 |
| `structured_lowbar_entry` | `reversion` | confirmed | 极端收盘位反转 |

结构化策略统一走 Why/When/Where/Volume 评分框架：`_why()` 和 `_when()` 是硬门控，`_where()` 和 `_volume_bonus()` 是软加分，`_entry_spec()` 和 `_exit_spec()` 分别输出入场与出场意图。策略参数优先通过 `config/signal.ini` 或本机 `config/signal.local.ini` 的 `[strategy_params]` / `[strategy_params.<TF>]` 调整。

注意：`config/*.local.ini` 会优先覆盖默认配置。若 `signal.local.ini` 中仍保留已删除 legacy 策略或旧投票组，会改变运行语义；详见 `docs/codebase-review.md`。

## 风险管理

交易请求经过多层校验（任一失败即拒绝）：

1. **信号过滤** — 时段/点差/经济事件/波动率异常
2. **策略内部 Regime 判断** — 结构化策略在 `_why()` 中判断是否适合当前市况
3. **置信度管线** — 结构化评分 × regime affinity × session performance × calibrator
4. **ExecutionGate** — 投票组/白名单/armed/intrabar 准入
5. **PendingEntry** — 按策略 `_entry_spec()` 执行市价、limit 或 stop 入场
6. **PreTradeRiskService** — 日损失限制、保证金、频率、经纪商约束
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
- `TradeExecutionReplayService`：负责 `execute_trade` 的幂等回放缓存与审计回放查询。
- `TradingModule`：保留为应用协调器，不再直接持有上述共享状态实现细节。

### 审计与 Trace

- `trade_command_audits`：交易命令审计表，只记录 `execute_trade / precheck_trade / close / cancel / modify` 等命令类动作。
- 查询类调用不再写入同一张审计表，避免把命令审计、健康探针和运行查询混成一个事实源。
- `GET /v1/trade/command-audits`：读取最近交易命令审计。
- `pipeline_trace_events`：PipelineEventBus 的持久化事实表，记录 `bar_closed / indicator_computed / snapshot_published / signal_filter_decided / signal_evaluated / execution_decided / execution_blocked / execution_submitted / pending_order_submitted / execution_failed` 主链路节点。
- `GET /v1/trade/trace/{signal_id}`：按 `signal_id` 聚合 `pipeline_trace_events / signal_events / auto_executions / trade_command_audits / pending_order_states / position_runtime_states / trade_outcomes`，输出从市场数据到交易结果的时间线与节点关系，供可视化与问题定位使用。
- `GET /v1/trade/trace/by-trace/{trace_id}`：按 pipeline `trace_id` 聚合同一条链路，覆盖“被过滤、尚未生成 signal_id”的排查场景。

模块布局规范见：[docs/architecture.md](docs/architecture.md)。

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
| [系统架构参考](docs/architecture.md) | 分层、启动流程、数据流、模块边界、布局规范 |
| [信号系统深度参考](docs/signal-system.md) | 策略列表、Intrabar 链路、投票引擎、置信度管线 |
| [价格确认入场](docs/design/pending-entry.md) | PendingEntry 机制设计方案（已实现） |
| [风控增强](docs/design/risk-enhancement.md) | PnL 熔断 + PerformanceTracker 恢复（已实现） |

## 许可

私有仓库，未公开授权。
