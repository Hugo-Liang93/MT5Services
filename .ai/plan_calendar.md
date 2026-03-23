# Calendar 模块增强方案

## 实施状态：已完成 (2026-03-23)

- Phase 1-4 全部实施完毕，722 测试全部通过
- 专家审视发现的 7 个问题全部修复（OHLC 索引、性能缓存、路由顺序等）

## 用户 Follow-up

1. 申请 FMP API Key: https://site.financialmodelingprep.com/developer/docs
2. 申请 Alpha Vantage API Key: https://www.alphavantage.co/support/#api-key
3. 写入 `config/economic.local.ini`：`[fmp] api_key = YOUR_KEY` / `[alphavantage] api_key = YOUR_KEY`
4. 在 `config/economic.ini` 设置 `[fmp] enabled = true` / `[alphavantage] enabled = true`
5. 重启服务后检查 `GET /economic/calendar/status`

## Context

当前经济日历模块有两个数据源（FRED + TradingEconomics），Provider 通过硬编码 if/elif 分支管理，无法灵活扩展。用户需要：
1. 引入更多权威数据源，获取更全面的市场数据
2. 统计重要经济数据发布前后的行情变化，未来用于策略输入和交易权重调整

本方案分 4 个 Phase 实施，Phase 1-2 解决数据源扩展，Phase 3-4 解决行情影响统计。

---

## Phase 1: Provider 抽象重构

### 目标
将硬编码的 if/elif provider 分支改为 Protocol + Registry 模式，支持任意数量的数据源。

### 1.1 定义 Provider Protocol

在 `src/clients/economic_calendar.py` 顶部新增：

```python
class EconomicCalendarProvider(Protocol):
    """经济日历数据源提供者协议。"""
    name: str

    def fetch_events(
        self, start_date: date, end_date: date,
        countries: Optional[List[str]] = None,
    ) -> List[EconomicCalendarEvent]: ...

    def supports_release_watch(self) -> bool: ...

    def is_configured(self) -> bool: ...
```

给现有 `TradingEconomicsCalendarClient` 和 `FredCalendarClient` 添加这三个属性/方法：
- `name = "tradingeconomics"` / `name = "fred"`
- `supports_release_watch()`: TE 返回 True，FRED 返回 False
- `is_configured()`: 检查 api_key 是否非空

### 1.2 创建 Provider Registry

**新建** `src/clients/economic_calendar_registry.py`：

```python
class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: Dict[str, EconomicCalendarProvider] = {}

    def register(self, provider: EconomicCalendarProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> Optional[EconomicCalendarProvider]: ...
    def all_names(self) -> List[str]: ...
    def configured_names(self) -> List[str]: ...
    def release_watch_names(self) -> List[str]: ...
```

### 1.3 重构 EconomicCalendarService

**修改** `src/calendar/service.py`：

| 当前代码 | 改为 |
|---------|------|
| `__init__` 直接实例化 `_te_client` / `_fred_client` | 接受 `ProviderRegistry` 参数 |
| `_provider_status` 硬编码两个 key | 从 registry 动态生成 |
| `_default_sources_for_job()` 硬编码列表 | release_watch → `registry.release_watch_names()`；其他 → `registry.configured_names()` |
| `_normalize_sources()` 硬编码 `{"tradingeconomics", "fred"}` | 改为 `set(registry.all_names())` |
| `_fetch_from_provider()` if/elif 分支 | `provider = registry.get(name); provider.fetch_events(...)` |

**修改** `src/calendar/economic_calendar/calendar_sync.py`：
- `fetch_job_events()` 中的 provider enabled 检查改为 `registry.get(provider).is_configured()` 判断

### 1.4 配置加载泛化

**修改** `src/config/models/runtime.py` — `EconomicConfig` 新增字段：

```python
# 新增 provider 配置（Phase 2 的各 provider 在此扩展）
fmp_enabled: bool = False
fmp_api_key: str | None = None
alphavantage_enabled: bool = False
alphavantage_api_key: str | None = None
```

**修改** `src/config/centralized.py`：
- 在 `economic_config` 合并逻辑中，从 `configs["economic"]` 中提取新 section（`[fmp]`, `[alphavantage]`）的 enabled/api_key 字段
- `_normalize_optional_secret()` 覆盖新 api_key 字段
- `effective_config_snapshot()` 中遮蔽新 api_key

### 1.5 工厂函数适配

**修改** `src/api/factories/trading.py`：
- 构建 `ProviderRegistry` 实例
- 根据 `EconomicConfig` 中各 provider 的 enabled/api_key 条件注册对应 client
- 将 registry 传入 `EconomicCalendarService`

### 涉及文件

| 文件 | 操作 |
|------|------|
| `src/clients/economic_calendar.py` | 修改：添加 Protocol；TE/FRED 适配 Protocol |
| `src/clients/economic_calendar_registry.py` | **新建** |
| `src/calendar/service.py` | 修改：重构为 Registry 模式 |
| `src/calendar/economic_calendar/calendar_sync.py` | 修改：provider enabled 检查逻辑 |
| `src/config/models/runtime.py` | 修改：新增 provider 配置字段 |
| `src/config/centralized.py` | 修改：泛化 provider section 加载 |
| `src/api/factories/trading.py` | 修改：构建 Registry |
| `tests/calendar/test_provider_registry.py` | **新建** |

---

## Phase 2: 新数据源实现

### 推荐数据源（按优先级排序）

#### 2.1 FMP (Financial Modeling Prep) — 最高优先级

| 属性 | 说明 |
|------|------|
| API | `GET https://financialmodelingprep.com/api/v3/economic_calendar?from=YYYY-MM-DD&to=YYYY-MM-DD&apikey=KEY` |
| 免费层 | 250 请求/天（足够 calendar_sync + near_term_sync） |
| 付费层 | Starter $14/月，300 请求/分钟 |
| XAUUSD 价值 | **高** — 全球经济日历，含 actual/forecast/previous + impact(Low/Medium/High)，时间精确到分钟 |
| 独特数据 | `changePercentage` 字段（actual vs estimate 偏差百分比），对行情影响统计直接可用 |
| release_watch | True（支持高频轮询检测 actual 值更新） |
| 适配复杂度 | **低** — 字段直接映射 EconomicCalendarEvent |

**字段映射**：
```
event → event_name
date → scheduled_at
country → country
impact → importance (High=3, Medium=2, Low=1)
actual/estimate/previous/change → actual/forecast/previous/revised
```

**申请地址**：https://site.financialmodelingprep.com/developer/docs

#### 2.2 Alpha Vantage Economic Indicators — 中等优先级

| 属性 | 说明 |
|------|------|
| API | `GET https://www.alphavantage.co/query?function=CPI&interval=monthly&apikey=KEY` (按指标查询) |
| 免费层 | 25 请求/天（每个指标一个请求） |
| 付费层 | Premium $49.99/月，75 请求/分钟 |
| XAUUSD 价值 | **中高** — 核心美国经济指标历史数值序列（GDP、CPI、NFP、联邦基金利率、失业率等） |
| 独特数据 | **深度历史数据**（数十年），非日历格式而是时间序列。与 Phase 3 的历史回测完美互补 |
| release_watch | False（不是日历 API，不支持实时轮询） |
| 适配复杂度 | **中** — 返回时间序列，需要与日历事件匹配后补充 actual/previous 值 |

**支持的经济指标函数**：
- `REAL_GDP`, `CPI`, `INFLATION`, `RETAIL_SALES`
- `FEDERAL_FUNDS_RATE`, `NONFARM_PAYROLL`, `UNEMPLOYMENT`
- `TREASURY_YIELD`, `CONSUMER_SENTIMENT`

**定位**：数据补充源 — 与 FRED/TE/FMP 的日历事件 cross-reference，补充精确的历史数值

**申请地址**：https://www.alphavantage.co/support/#api-key

### INI 配置新增

```ini
# config/economic.ini 新增 section

[fmp]
enabled = false
# FMP API Key（申请地址：https://site.financialmodelingprep.com/developer/docs）
# 建议写入 config/economic.local.ini
api_key =

[alphavantage]
enabled = false
# Alpha Vantage API Key（申请地址：https://www.alphavantage.co/support/#api-key）
# 建议写入 config/economic.local.ini
api_key =
# 需要追踪的经济指标列表
tracked_indicators = CPI,NONFARM_PAYROLL,FEDERAL_FUNDS_RATE,UNEMPLOYMENT,REAL_GDP,RETAIL_SALES
```

### 新建 Client 文件

不拆分现有文件（避免大量 import 变更），直接在 `src/clients/` 下新增：

| 文件 | 说明 |
|------|------|
| `src/clients/fmp_calendar.py` | FmpCalendarClient，实现 EconomicCalendarProvider Protocol |
| `src/clients/alphavantage_calendar.py` | AlphaVantageClient，实现 EconomicCalendarProvider Protocol |
| `tests/clients/test_fmp_client.py` | **新建**：Mock HTTP 测试 FMP 事件解析 |
| `tests/clients/test_alphavantage_client.py` | **新建**：Mock HTTP 测试 AV 数据解析 |

### 用户 Follow-up

完成 Phase 2 后需要用户：
1. 申请 FMP API Key → 写入 `config/economic.local.ini` 的 `[fmp] api_key = YOUR_KEY`
2. 申请 Alpha Vantage API Key → 写入 `config/economic.local.ini` 的 `[alphavantage] api_key = YOUR_KEY`
3. 在 `config/economic.ini` 中设置 `enabled = true`

---

## Phase 3: 经济数据发布前后行情影响统计

### 目标
自动统计每个高重要性经济事件发布前后的市场价格变化、波动率跳变，并聚合成历史模式。

### 3.1 新表：`economic_event_market_impact`

**新建** `src/persistence/schema/economic_event_market_impact.py`

```sql
CREATE TABLE IF NOT EXISTS economic_event_market_impact (
    recorded_at         timestamptz NOT NULL,          -- 记录时间
    event_uid           text NOT NULL,                 -- 事件唯一标识
    symbol              text NOT NULL,                 -- 交易品种
    timeframe           text NOT NULL,                 -- K线周期

    -- 事件信息（冗余，方便聚合查询）
    event_name          text NOT NULL,
    country             text,
    currency            text,
    importance          integer,
    scheduled_at        timestamptz NOT NULL,           -- 计划发布时间
    released_at         timestamptz,                    -- 实际发布时间

    -- Actual vs Forecast 偏差
    actual              text,
    forecast            text,
    previous            text,
    surprise_pct        double precision,               -- (actual-forecast)/|forecast|*100

    -- 发布前行情（各时间窗口）
    pre_price           double precision,               -- 事件发布时刻价格（参考基准）
    pre_30m_change      double precision,               -- 发布前30min价格变化
    pre_30m_range       double precision,               -- 发布前30min high-low
    pre_60m_change      double precision,
    pre_60m_range       double precision,
    pre_120m_change     double precision,
    pre_120m_range      double precision,

    -- 发布后行情（各时间窗口）
    post_5m_change      double precision,               -- 发布后5min价格变化
    post_5m_range       double precision,
    post_15m_change     double precision,
    post_15m_range      double precision,
    post_30m_change     double precision,
    post_30m_range      double precision,
    post_60m_change     double precision,
    post_60m_range      double precision,
    post_120m_change    double precision,
    post_120m_range     double precision,

    -- 波动率跳变
    volatility_pre_atr  double precision,               -- 事件前ATR
    volatility_post_atr double precision,               -- 事件后ATR
    volatility_spike    double precision,               -- post/pre 比率

    -- 分析状态
    analysis_status     text NOT NULL DEFAULT 'pending', -- pending/partial/complete/skipped/error
    metadata            jsonb,

    PRIMARY KEY (event_uid, symbol, timeframe)
);

-- TimescaleDB hypertable
SELECT create_hypertable('economic_event_market_impact', 'recorded_at',
                          if_not_exists => TRUE, migrate_data => TRUE);

-- 索引
CREATE INDEX IF NOT EXISTS eemi_event_idx ON economic_event_market_impact (event_name, symbol, recorded_at DESC);
CREATE INDEX IF NOT EXISTS eemi_country_idx ON economic_event_market_impact (country, importance DESC, recorded_at DESC);
CREATE INDEX IF NOT EXISTS eemi_status_idx ON economic_event_market_impact (analysis_status, recorded_at DESC);
```

### 3.2 核心服务：MarketImpactAnalyzer

**新建** `src/calendar/economic_calendar/market_impact.py`

```python
class MarketImpactAnalyzer:
    """经济事件行情影响分析器。

    职责：
    1. 事件 released 后，从 market_repo 收集前后行情数据
    2. 计算价格变化、波动率跳变、surprise 偏差
    3. 持久化到 economic_event_market_impact 表
    4. 提供历史聚合统计查询
    """

    def __init__(self, db_writer, market_repo, storage_writer=None, settings=None): ...

    # === 数据收集 ===

    def on_event_released(self, event: EconomicCalendarEvent) -> None:
        """release_watch 检测到事件 released 时调用，注册延迟收集任务。"""

    def tick(self) -> None:
        """在 release_watch 每轮轮询中调用，检查并执行到期的收集任务。"""

    def collect_impact(self, event, symbol, timeframe) -> dict:
        """从 market_repo 拉取事件前后 OHLC 数据，计算影响指标。

        数据源：market_repo.fetch_ohlc_range(symbol, tf, start, end)
        基准价格：事件 scheduled_at 时刻最近一根 K 线的 close
        各窗口 change = window_close - pre_price
        各窗口 range = max(highs) - min(lows)
        surprise_pct = (actual - forecast) / |forecast| * 100
        volatility_spike = post_atr / pre_atr
        """

    # === 分阶段收集 ===
    # pending → 事件刚 released，pre 窗口数据已可用
    # partial → post 短窗口(5m/15m)已收集，等待长窗口
    # complete → 所有 post 窗口收集完毕（最长 120min 后）

    # === 查询接口 ===

    def get_event_impact(self, event_uid, symbol) -> Optional[dict]:
        """查询单个事件的行情影响详情。"""

    def get_aggregated_stats(self, event_name=None, country=None,
                             importance_min=None, symbol="XAUUSD") -> list[dict]:
        """聚合统计：按事件类型分组的历史平均影响。

        返回：
        - avg_post_Nm_change: 各窗口平均价格变化
        - avg_post_Nm_range: 各窗口平均波动幅度
        - bullish_pct: 上涨概率
        - surprise_correlation: surprise 与 post change 的相关系数
        - avg_volatility_spike: 平均波动率跳变倍数
        - sample_count: 样本数
        """

    def get_impact_forecast(self, event_name, symbol="XAUUSD") -> Optional[dict]:
        """为策略/Trade Guard 提供的简化接口。

        返回：
        {
            "expected_volatility_spike": 2.5,
            "directional_bias": 0.65,       # 上涨概率
            "avg_post_30m_range": 15.3,     # 平均30min波动幅度(点)
            "sample_count": 24,
        }
        """
```

### 3.3 触发机制

利用现有 `release_watch` job 作为入口：

```
release_watch 每 60s 轮询
    ↓
calendar_sync.write_events() 检测 status → "released"
    ↓  回调
MarketImpactAnalyzer.on_event_released(event)
    ↓
注册 pending 收集任务（记录 event_uid + scheduled_at）
    ↓
release_watch 下一轮 → analyzer.tick()
    ↓  检查到期的 pending 任务
collect_impact(event, symbol, timeframe)
    ↓
market_repo.fetch_ohlc_range(symbol, "M1", scheduled_at - 120min, scheduled_at + 120min)
    ↓
计算各窗口指标 → 写入 economic_event_market_impact 表
```

**分阶段调度**：
- 事件 released 后立即：填充 pre 窗口数据 + 标记 `pending`
- +5min：回填 post_5m
- +15min：回填 post_15m
- +30min：回填 post_30m
- +60min：回填 post_60m
- +130min（final_collection_delay）：回填 post_120m + volatility_spike → 标记 `complete`

### 3.4 配置

`config/economic.ini` 新增 section：

```ini
[market_impact]
# 是否启用行情影响统计
enabled = true

# 统计的品种（逗号分隔）
symbols = XAUUSD

# 统计的时间框架（M1 精度最高）
timeframes = M1,M5

# 仅统计 importance >= 此阈值的事件
importance_min = 2

# 发布前回溯窗口（分钟，逗号分隔）
pre_windows = 30,60,120

# 发布后观察窗口（分钟，逗号分隔）
post_windows = 5,15,30,60,120

# 最终收集延迟（分钟）— 所有 post 窗口结束后额外等待
final_collection_delay_minutes = 130

# 历史回填：启动时回填过去 N 天内已 released 但未统计的事件
backfill_enabled = true
backfill_days = 30

# 聚合统计刷新间隔（秒，0=不自动刷新，手动通过 API 触发）
stats_refresh_interval_seconds = 21600
```

**EconomicConfig 扩展**（`src/config/models/runtime.py`）：

```python
# market_impact 配置字段
market_impact_enabled: bool = False
market_impact_symbols: List[str] = Field(default_factory=lambda: ["XAUUSD"])
market_impact_timeframes: List[str] = Field(default_factory=lambda: ["M1", "M5"])
market_impact_importance_min: int = 2
market_impact_pre_windows: List[int] = Field(default_factory=lambda: [30, 60, 120])
market_impact_post_windows: List[int] = Field(default_factory=lambda: [5, 15, 30, 60, 120])
market_impact_final_collection_delay_minutes: int = 130
market_impact_backfill_enabled: bool = True
market_impact_backfill_days: int = 30
market_impact_stats_refresh_interval_seconds: float = 21600.0
```

### 3.5 API 端点

**修改** `src/api/economic.py` 新增 3 个端点：

```python
GET /economic/calendar/market-impact/{event_uid}
    ?symbol=XAUUSD
    → 单个事件的行情影响详情（pre/post 各窗口数据）

GET /economic/calendar/market-impact/stats
    ?event_name=Nonfarm Payrolls&country=United States
    &importance_min=3&symbol=XAUUSD&timeframe=M5&limit=50
    → 聚合统计：按事件类型分组的历史平均影响

GET /economic/calendar/market-impact/upcoming
    ?symbol=XAUUSD&hours=24
    → 即将到来的事件 + 基于历史统计的预期影响
```

### 3.6 存储通道

**修改** `config/storage.ini` 和 `src/persistence/storage_writer.py`：
- 新增 `economic_event_market_impact` 通道（queue_size=2000, flush_interval=5s, batch_size=100, overflow=drop_newest）

### 涉及文件

| 文件 | 操作 |
|------|------|
| `src/persistence/schema/economic_event_market_impact.py` | **新建**：表 DDL |
| `src/persistence/schema/__init__.py` | 修改：注册新表 |
| `src/persistence/repositories/economic_repo.py` | 修改：新增 market_impact 读写方法 |
| `src/persistence/db.py` | 修改：代理新仓储方法 |
| `src/calendar/economic_calendar/market_impact.py` | **新建**：核心分析器 |
| `src/calendar/service.py` | 修改：集成 MarketImpactAnalyzer |
| `src/calendar/economic_calendar/calendar_sync.py` | 修改：released 事件触发回调 |
| `src/config/models/runtime.py` | 修改：新增 market_impact 配置字段 |
| `src/config/centralized.py` | 修改：加载 [market_impact] section |
| `config/economic.ini` | 修改：新增 [market_impact] section |
| `config/storage.ini` | 修改：新增通道配置 |
| `src/persistence/storage_writer.py` | 修改：新增通道映射 |
| `src/api/economic.py` | 修改：新增 3 个 API 端点 |
| `src/api/deps.py` | 修改：注入 MarketImpactAnalyzer |
| `src/api/factories/trading.py` | 修改：构建 MarketImpactAnalyzer |
| `tests/calendar/test_market_impact.py` | **新建**：计算逻辑测试 |
| `tests/calendar/test_market_impact_collection.py` | **新建**：分阶段收集测试 |

---

## Phase 4: 策略与 Trade Guard 集成

### 目标
让行情影响统计数据可被信号策略和 Trade Guard 消费，实现数据驱动的风控决策。纳入本次实施范围，与 Phase 3 一起交付。

### 4.1 SignalContext 扩展

**修改** `src/signals/models.py`：

```python
@dataclass
class SignalContext:
    # ... 现有字段 ...
    event_impact_forecast: Optional[Dict[str, Any]] = None  # 新增
```

### 4.2 SignalRuntime 注入

**修改** `src/signals/orchestration/runtime.py`：
- 评估前查询 `MarketImpactAnalyzer.get_impact_forecast(upcoming_event_name, symbol)`
- 有高波动率预期事件时注入 context，策略可按需调整置信度

### 4.3 Trade Guard 动态保护窗口

**修改** `src/calendar/economic_calendar/trade_guard.py`：
- `get_trade_guard()` 中查询事件的历史波动率统计
- 高波动事件（`volatility_spike > 3.0`）自动扩大 pre/post buffer

### 涉及文件

| 文件 | 操作 |
|------|------|
| `src/signals/models.py` | 修改：SignalContext 新增字段 |
| `src/signals/orchestration/runtime.py` | 修改：注入 event impact |
| `src/calendar/economic_calendar/trade_guard.py` | 修改：动态 buffer |
| `tests/signals/test_event_impact_context.py` | **新建** |
| `tests/calendar/test_trade_guard_dynamic.py` | **新建** |

> **注意**：Phase 4 的效果依赖 Phase 3 积累的历史数据。代码一次性交付，但统计功能的实际价值会随数据积累逐步提升。启动时如果 backfill 开启，会自动回填过去 30 天的已发布事件。

---

## 实施顺序与依赖

```
Phase 1 (Provider 抽象) ──── 无依赖，立即开始
    ↓
Phase 2 (新数据源) + Phase 3 (行情统计) ──── 并行开发
    ↓
Phase 4 (策略集成) ──── 依赖 Phase 3 的 MarketImpactAnalyzer
```

**确认的实施路径**：Phase 1 → Phase 2 + Phase 3 并行 → Phase 4（全部纳入本次实施）

---

## 用户 Follow-up 清单

完成实施后，用户需要：

1. **申请 API Key**：
   - FMP: https://site.financialmodelingprep.com/developer/docs （免费 250 请求/天）
   - Alpha Vantage: https://www.alphavantage.co/support/#api-key （免费 25 请求/天）

2. **配置 API Key**（写入 `config/economic.local.ini`）：
   ```ini
   [fmp]
   api_key = YOUR_FMP_KEY

   [alphavantage]
   api_key = YOUR_ALPHAVANTAGE_KEY
   ```

3. **启用新数据源**（在 `config/economic.ini` 中设置）：
   ```ini
   [fmp]
   enabled = true

   [alphavantage]
   enabled = true
   ```

4. **验证**：重启服务后检查 `GET /economic/calendar/status` 确认新 provider 正常工作

---

## 验证计划

### Phase 1 验证
```bash
pytest tests/calendar/test_provider_registry.py -v
# 确认现有 TE/FRED 通过 Registry 正常工作
# 手动 GET /economic/calendar/status 检查 provider_status
```

### Phase 2 验证
```bash
pytest tests/clients/test_fmp_client.py -v
pytest tests/clients/test_alphavantage_client.py -v
# 配入 API Key 后手动触发 POST /economic/calendar/refresh?sources=fmp
# 检查 GET /economic/calendar?sources=fmp 有数据返回
```

### Phase 3 验证
```bash
pytest tests/calendar/test_market_impact.py -v
pytest tests/calendar/test_market_impact_collection.py -v
# 等待一个高重要性事件发布
# 检查 GET /economic/calendar/market-impact/stats?importance_min=3
# 或手动回填：POST /economic/calendar/refresh 后检查 backfill 日志
```

### Phase 4 验证
```bash
pytest tests/signals/test_event_impact_context.py -v
pytest tests/calendar/test_trade_guard_dynamic.py -v
# 在有即将到来的高重要性事件时检查 Trade Guard 是否动态扩大保护窗口
```
