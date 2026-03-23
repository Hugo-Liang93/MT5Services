# MT5Services

Config rules:
- Runtime code should read config through `src.config`.
- `config/indicators.json` is the only indicator config entrypoint.
- Runtime does not expand new compatibility layers; add formal modules and formal config loaders instead.
- Architecture and domain boundaries are documented in `ARCHITECTURE.md`.

MT5Services 是一个基于 FastAPI 的统一运行服务，围绕 MetaTrader 5 提供行情采集、历史落库、指标计算、信号生成、账户与交易接口、经济日历风控和系统监控。

当前仓库已经收敛为单一运行模式，默认入口是 `python app.py`，不再区分多套启动方式。

## 功能概览

- 市场数据：`quote`、`ticks`、`ohlc`、盘中 OHLC 序列、SSE 流订阅
- 持久化：Ticks、Quotes、OHLC、指标结果、经济日历事件、运行任务状态
- 指标系统：`config/indicators.json` + `src/indicators/manager.py`，事件驱动、依赖图调度
- 信号系统：多策略评估、Regime 检测、多策略投票引擎、置信度校准、历史绩效追踪
- 交易能力：账户查询、持仓/挂单查询、下单、平仓、改单、保证金预估、自动信号执行
- 宏观风控：经济日历抓取、事件筛选、风险时间窗、交易前检查
- 系统监控：健康检查、队列状态、性能指标、启动阶段、有效配置快照

## 运行架构

统一启动链路如下：

1. `app.py` 解析 Host/Port 并启动 `uvicorn src.api:app`
2. `src/api/__init__.py` 创建 FastAPI 应用并注册全部路由
3. `src/app_runtime/` 构建并启动核心组件（container → builder → runtime）

默认启动的核心组件：

- `MarketDataService`
- `StorageWriter`
- `BackgroundIngestor`
- `EconomicCalendarService`
- `UnifiedIndicatorManager`
- `MarketStructureAnalyzer`
- `SignalModule`
- `SignalRuntime`
- `SignalQualityTracker` / `TradeOutcomeTracker`
- `TradeExecutor` / `PositionManager`
- `TradingModule`
- `HealthMonitor` / `MonitoringManager`

启动顺序：

1. `storage`
2. `ingestion`
3. `economic_calendar`
4. `indicators`
5. `signals`
6. `position_manager`
7. `monitoring`

运行时可通过 `GET /monitoring/startup` 和 `GET /monitoring/runtime-tasks` 查看阶段状态。

### 数据流概览

```
MT5 Terminal
    ↓ （后台线程）
BackgroundIngestor
    ├─ MarketDataService（内存缓存，RLock 保护）
    └─ StorageWriter（多通道队列）→ TimescaleDB

OHLC 收盘事件
    → UnifiedIndicatorManager（依赖图计算）
        → confirmed/intrabar 快照
            → SignalRuntime（双队列事件处理）
                ├─ Regime 检测
                ├─ 策略评估 × Regime 亲和度修正
                ├─ ConfidenceCalibrator（历史胜率校准）
                ├─ VotingEngine（跨策略投票）
                ├─ SignalQualityTracker（confirmed 信号 N bar 后质量回填）
                ├─ TradeOutcomeTracker（真实成交结果回填）
                └─ TradeExecutor（confirmed 信号自动执行）
```

## 目录结构

```text
MT5Services/
├─ app.py
├─ config/
│  ├─ app.ini           # 交易品种、时间框架、采集间隔
│  ├─ market.ini        # API host/port、认证、CORS
│  ├─ ingest.ini        # 采集节奏、性能、健康阈值
│  ├─ storage.ini       # 存储通道队列与 flush 策略
│  ├─ economic.ini      # 日历抓取、事件筛选、交易风控
│  ├─ mt5.ini           # MT5 连接参数与账户配置
│  ├─ db.ini            # 数据库连接
│  ├─ risk.ini          # 风险限制（仓位数量、SL/TP 要求）
│  ├─ cache.ini         # 运行时内存缓存覆盖
│  ├─ signal.ini        # 信号状态机、自动交易、投票、市场结构与执行参数
│  └─ indicators.json   # 指标定义与计算流水线
├─ src/
│  ├─ app_runtime/      # 应用运行时（container/builder/runtime 三层分离）
│  ├─ api/              # FastAPI 路由、中间件、Schema、DI 适配层
│  ├─ calendar/         # EconomicCalendarService 与 trade guard
│  ├─ clients/          # MT5 客户端封装（行情、交易、账户、统一时间 API）
│  ├─ config/           # 配置加载、合并、Pydantic 模型
│  ├─ indicators/       # 统一指标系统（管理器、引擎、缓存）
│  ├─ ingestion/        # 后台 Tick/OHLC/Intrabar 数据采集
│  ├─ market/           # MarketDataService 运行时行情缓存
│  ├─ market_structure/ # 市场结构上下文分析
│  ├─ monitoring/       # 健康检查
│  ├─ persistence/      # TimescaleDB 写入器、队列持久化
│  ├─ risk/             # 风险规则、模型、服务
│  ├─ signals/          # 信号生成策略、运行时、过滤器
│  ├─ trading/          # TradingModule、账户注册、信号执行器
│  └─ utils/            # 通用工具、事件存储、统一时区模块
├─ tests/
│  ├─ api/
│  ├─ config/
│  ├─ core/
│  ├─ data/
│  ├─ indicators/
│  ├─ signals/
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
- `config/signal.ini`
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

## 指标系统

指标在 `config/indicators.json` 中声明式配置，`UnifiedIndicatorManager` 负责编排：

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

计算模式：

| 模式 | 说明 |
|------|------|
| `standard` | 每次 bar 收盘时全量重计算 |
| `incremental` | 仅追加更新，流式场景更快 |
| `parallel` | 与其他指标并行计算 |

指标结果以快照形式通知订阅者，scope 分为 `confirmed`（K 线收盘）和 `intrabar`（盘中实时）。

新增指标步骤：

1. 在 `src/indicators/core/` 中实现函数
2. 在 `config/indicators.json` 中添加条目（填写 `func_path`、`params`、`dependencies`）
3. 在 `tests/indicators/` 中添加测试

## 信号系统

信号系统由以下组件协作完成从指标快照到交易决策的全链路处理。

### 内置策略

| 策略名 | 类 | 所需指标 | Scope | 适合 Regime |
|--------|-----|---------|-------|------------|
| `sma_trend` | SmaTrendStrategy | sma20, ema50 | confirmed | 趋势 |
| `rsi_reversion` | RsiReversionStrategy | rsi14 | intrabar, confirmed | 震荡 |
| `bollinger_breakout` | BollingerBreakoutStrategy | boll20 | intrabar, confirmed | 突破 |
| `supertrend` | SupertrendStrategy | supertrend14, adx14 | confirmed | 趋势 |
| `stoch_rsi` | StochRsiStrategy | stoch_rsi14 | intrabar, confirmed | 震荡 |
| `macd_momentum` | MacdMomentumStrategy | macd | confirmed | 趋势/突破 |
| `keltner_bb_squeeze` | KeltnerBollingerSqueezeStrategy | boll20, keltner20 | intrabar, confirmed | 突破 |
| `donchian_breakout` | DonchianBreakoutStrategy | donchian20, adx14 | confirmed | 突破/趋势 |
| `fake_breakout` | FakeBreakoutDetector | donchian20, atr14 | confirmed | 震荡/假突破 |
| `squeeze_release` | SqueezeReleaseFollow | boll20, keltner20, macd | confirmed | 突破 |
| `session_momentum` | SessionMomentumBias | atr14, supertrend14 | confirmed | 趋势/时段动量 |
| `price_action_reversal` | PriceActionReversal | atr14 | confirmed | 反转/震荡 |
| `multi_timeframe_confirm` | MultiTimeframeConfirmStrategy | sma20, ema50 | confirmed | 趋势 |

复合策略（`src/signals/strategies/composite.py`）可在此基础上组合多指标本地确认逻辑，通过 `src/signals/strategies/registry.py` 注册。

当前默认还加载以下复合策略：

- `breakout_release_confirm`: `donchian_breakout + squeeze_release`
- `reversal_rejection_confirm`: `fake_breakout + price_action_reversal`

### Regime 检测

`src/signals/evaluation/regime.py` 对每根 K 线收盘计算行情状态（优先级从高到低）：

1. Keltner-Bollinger Squeeze（BB 完全在 KC 内）→ `BREAKOUT`
2. ADX ≥ 23 → `TRENDING`
3. ADX < 18 且 BB 宽度 < 0.8% → `BREAKOUT`（盘整蓄力）
4. ADX < 18 → `RANGING`
5. 18 ≤ ADX < 23 → `UNCERTAIN`
6. 无指标数据 → `UNCERTAIN`（兜底）

Regime 结果以**亲和度乘数**修正策略置信度：`adjusted_confidence = raw_confidence × affinity`。
当 `adjusted_confidence < min_preview_confidence`（默认 0.55）时信号被静默，无需在策略内手动判断 Regime。

启用 soft regime 后，运行时 metadata 会包含 `regime_source`、`regime_probabilities` 和 `dominant_regime_probability`，策略 affinity 按概率分布加权，不再只依赖单一硬分类。

### 信号状态机

SignalRuntime 使用**双队列**防止高频 intrabar 事件挤占 confirmed 事件：

| 队列 | 大小 | 优先级 |
|------|------|--------|
| `_confirmed_events` | 4096 | 高（优先消费） |
| `_intrabar_events` | 8192 | 低（confirmed 为空时消费） |

状态机转移：

- **intrabar scope**：`idle` → `preview_buy/sell` → `armed_buy/sell` → `cancelled`
- **confirmed scope**：`idle` → `confirmed_buy/sell` → `idle`

`confirmed_buy/sell` 信号触发 TradeExecutor 自动开仓（若 `auto_trade_enabled = true`）。

Before voting, runtime now fuses duplicate strategy outputs within the same `(symbol, timeframe, strategy, bar_time)`: `confirmed` overrides `intrabar`, same-scope duplicates keep the latest result, and one strategy contributes at most one vote per bar.

### 置信度校准

`ConfidenceCalibrator`（`src/signals/evaluation/calibrator.py`）根据历史胜率动态调整策略置信度：

- `SignalQualityTracker` 在 confirmed 信号产生后 N 根 bar 回填理论胜负
- `TradeOutcomeTracker` 在真实仓位关闭后回填实际盈亏结果
- 运行时默认使用较轻的混合参数：`alpha=0.15`、`min_samples=50`、`recency_hours=8`
- 分阶段校准已启用：`<50` 样本不校准，`50-99` 使用 `warmup_alpha=0.10`，`100+` 使用 `alpha=0.15`
- 近期窗口表现弱于基准时只允许压制，不再放大置信度
- 历史绩效存储在 `DB:signal_outcomes`

动量类指标还支持 `delta_bars` 快照扩展，例如 `rsi_d3`、`rsi_d5`、`macd_d3`、`adx_d5`，用于表达 3/5 根 bar 的变化速率。

### 多策略投票引擎

`VotingEngine`（`src/signals/orchestration/voting.py`）汇总同一快照内多个策略的输出：

- 加权 buy/sell 得分 > threshold 且满足 quorum → 产生 `strategy="consensus"` 信号
- 方向分歧越大，共识置信度折扣越多
- 投票统计可通过 `GET /signals/voting/stats` 查询
- 当前默认分组：
  `trend_vote = supertrend, ema_ribbon, macd_momentum, hma_cross, session_momentum`
  `reversion_vote = rsi_reversion, stoch_rsi, williams_r, cci_reversion, price_action_reversal`
  `breakout_vote = bollinger_breakout, donchian_breakout, keltner_bb_squeeze, squeeze_release, fake_breakout`

### 新增策略 SOP

1. 在对应策略模块中实现策略类，声明四个必填属性：
   `src/signals/strategies/trend.py`、`src/signals/strategies/mean_reversion.py`、`src/signals/strategies/breakout.py`、`src/signals/strategies/session.py`、`src/signals/strategies/price_action.py` 或 `src/signals/strategies/composite.py`

   ```python
   class MyStrategy:
       name = "my_strategy"
       required_indicators = ("adx14", "sma20")
       preferred_scopes = ("confirmed",)
       regime_affinity = {
           RegimeType.TRENDING:  1.00,
           RegimeType.RANGING:   0.20,
           RegimeType.BREAKOUT:  0.50,
           RegimeType.UNCERTAIN: 0.50,
       }

       def evaluate(self, indicators, metadata) -> SignalDecision: ...
   ```

2. 在 `src/signals/service.py` 的默认策略列表中注册实例
3. 在 `tests/signals/` 中添加单元测试，覆盖四种 Regime 下的输出
4. （可选）在 `config/signal.ini` 中调整 `min_preview_confidence` / `cooldown_seconds`

## 风险管理

交易请求从内到外经过多层校验：

| 层次 | 模块 | 拦截条件 |
|------|------|---------|
| 信号过滤 | `src/signals/execution/filters.py` | 非交易时段、点差过大、高影响经济事件窗口、时段切换冷却 |
| Regime 亲和度 | `src/signals/evaluation/regime.py` | 策略在当前 Regime 的适配度不足 |
| 置信度阈值 | `src/signals/orchestration/runtime.py` | `adjusted_confidence < min_preview_confidence` |
| 交易前风险 | `src/risk/service.py` | 仓位总数超限、账户余额不足、日损失限制 |
| 风险规则 | `src/risk/rules.py` | 手数超限、SL/TP 未设或距离不足 |
| Trade Guard | `src/calendar/service.py` + `src/calendar/economic_calendar/trade_guard.py` | 经济事件窗口内阻止下单 |

## 核心接口

### 基础状态

- `GET /health`
- `GET /monitoring/health`
- `GET /monitoring/trading`
- `GET /monitoring/components`
- `GET /monitoring/trading/trigger-methods`
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

### 指标系统

- `GET /indicators/list`
- `GET /indicators/{symbol}/{timeframe}`
- `GET /indicators/{symbol}/{timeframe}/{indicator_name}`
- `POST /indicators/compute`
- `GET /indicators/performance/stats`
- `POST /indicators/cache/clear`
- `GET /indicators/dependency/graph`

### 信号系统

- `GET /signals/strategies` — 已注册的策略列表
- `POST /signals/evaluate` — 手动触发策略评估
- `GET /signals/recent` — 最近 N 个信号事件
- `GET /signals/summary` — 按策略/品种/时间框架汇总统计
- `GET /signals/best` — 每个 `(symbol, timeframe)` 当前最强 confirmed 信号
- `GET /signals/runtime/status` — SignalRuntime 状态快照
- `GET /signals/positions` — 信号驱动的持仓列表
- `GET /signals/regime/{symbol}/{timeframe}` — 当前 Regime 状态
- `GET /signals/market-structure/{symbol}/{timeframe}` — 当前结构分析快照
- `GET /signals/consensus/recent` — 最近的 consensus 共识信号
- `GET /signals/voting/stats` — 投票引擎统计
- `GET /signals/outcomes/winrate` — 策略历史胜率
- `GET /signals/htf/cache` — 高时间框架方向缓存状态
- `GET /signals/calibrator/status` — 置信度校准器状态
- `POST /signals/calibrator/refresh` — 手动刷新校准数据
- `GET /signals/strategies/composite` — 复合策略列表
- `GET /signals/diagnostics/strategy-conflicts` — 策略冲突诊断
- `GET /signals/diagnostics/daily-report` — 日质量报告
- `GET /signals/diagnostics/aggregate-summary` — 聚合诊断摘要
- `GET /signals/monitoring/quality/{symbol}/{timeframe}` — Regime + confirmed 信号质量监控

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

### 信号触发交易（交易模块职责）

- `POST /trade/from-signal` — 依据 `signal_id` 在交易模块中执行下单
- `GET /trade/control` — 查看自动开仓/close-only 控制状态与执行器断路器状态
- `POST /trade/control` — 暂停自动开仓、切换只平不开，并可重置执行断路器
- `POST /trade/reconcile` — 手动同步当前 MT5 持仓到 `PositionManager`

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
| `config/risk.ini` | 仓位数量限制、SL/TP 最小要求、最终执行 session 风控 |
| `config/cache.ini` | 运行时内存缓存覆盖（优先级高于 `app.ini [limits]`） |
| `config/signal.ini` | 信号状态机、自动交易开关、仓位大小、置信度阈值、投票/熔断/分时段 spread 与策略路由 |
| `config/indicators.json` | 当前统一指标配置 |

更细的说明见 [CONFIG_GUIDE.md](CONFIG_GUIDE.md)。

## 测试与质量工具

当前 `tests/` 已按目录拆分：

- `tests/api`
- `tests/config`
- `tests/core`
- `tests/data`
- `tests/indicators`
- `tests/signals`
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

## 运行时安全补充

- `config/risk.ini`
  - `daily_loss_limit_pct = 3.0` blocks new trades once the account reaches the configured daily-loss threshold.
  - `require_sl_for_market_orders = true` requires an SL on market orders by default.
- `config/economic.ini`
  - `trade_guard_mode = block` blocks new XAUUSD entries during active high-impact economic risk windows by default.
- `config/signal.ini`
  - `max_concurrent_positions_per_symbol = 3` limits concurrent signal-driven positions per symbol.
  - `session_transition_cooldown_minutes = 15` suppresses signal evaluation around the London -> New York handoff.
  - `end_of_day_close_enabled = true` with `end_of_day_close_hour_utc = 21` and `end_of_day_close_minute_utc = 0` enables end-of-day closeout in `PositionManager`.
  - `market_structure_m1_lookback_bars = 120` keeps M1 market-structure analysis focused on intraday context.
- runtime recovery
  - startup automatically runs `PositionManager.sync_open_positions()` to rehydrate tracked XAUUSD positions after restart.
  - trade execution carries `request_id` into MT5 comments, allowing retry-time state recovery and duplicate-order protection.
- `TradeExecutor` now applies timeframe-specific ATR sizing profiles:
  - `M1 = 1.0 / 2.0`
  - `M5 = 1.2 / 2.5`
  - `M15 = 1.3 / 2.8`
  - `H1 = 1.5 / 3.0`
- API additions:
  - `GET /signals/monitoring/quality/{symbol}/{timeframe}` returns regime diagnostics plus daily signal-quality summary.
  - risk-blocked trade APIs now return `daily_loss_limit` when the daily loss rule is the blocking cause.

如果文档与当前源码实现不一致，以 `app.py`、`src/api/`、`src/config/`、`src/indicators/manager.py`、`src/signals/orchestration/runtime.py` 为准。
