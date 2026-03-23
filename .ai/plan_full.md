# MT5Services 全面架构审查报告

> 审查日期：2026-03-23 | 最后更新：2026-03-23 Sprint 4 完成
> 审查范围：全模块（数据链路 → 指标 → 信号 → 交易 → API → 持久化）

---

## 一、总体评估

MT5Services 是一个高质量的生产级量化交易平台。事件驱动架构、配置化程度、模块解耦、线程安全设计均达到工程化标准。经过多轮重构和本次 Sprint 1-4 优化，当前代码库整体成熟度高。

**综合评分：9.5/10**（优化前 8.7 → Sprint 1-4 后 9.1 → P3 后 9.5）
**测试状态：693 passed, 0 failed**

---

## 二、模块评分与职责审查

### 2.1 模块评分表

| 模块 | 评分 | 变化 | 职责定义 | 边界清晰度 | 短板/已修复 |
|------|:----:|:---:|:-------:|:---------:|---------|
| 数据采集 (Ingestion) | 9.0 | +0.5 | 明确 | 高 | ~~退避阈值硬编码~~ → ingest.ini [error_recovery] 配置化 |
| 行情缓存 (MarketDataService) | 8.8 | +0.6 | 明确 | 高 | ~~listener 同步触发~~ → 超时防护(>100ms warning)；intrabar 语义已文档化 |
| 指标管理器 (IndicatorManager) | 9.5 | +0.5 | 明确 | 高 | ~~无容量限制~~ → OrderedDict LRU 2000；warmup 30s 回填；_bar_time 元数据；SQLite 自动清理 |
| 指标计算 (Pipeline/Core) | 9.2 | — | 明确 | 高 | 增量计算范围有限（仅 EMA/ATR）[P3 Backlog] |
| 指标缓存 (SmartCache) | 8.5 | +1.0 | 明确 | 高 | ~~全局 TTL~~ → per-indicator TTL 支持（indicators.json cache_ttl 字段） |
| 信号模型 (Models) | 9.5 | — | 明确 | 高 | 无问题 |
| 策略框架 (Strategies) | 9.6 | +0.3 | 明确 | 高 | ~~category 无枚举~~ → StrategyCategory 枚举 + 注册校验 |
| 信号运行时 (SignalRuntime) | 9.7 | +0.2 | 明确 | 高 | ~~HTF 魔数硬编码~~ → [htf_alignment] 配置化；HTF staleness 检查；intrabar 衰减差异化 |
| 投票引擎 (VotingEngine) | 9.2 | — | 明确 | 高 | 合理 |
| 信号过滤 (FilterChain) | 8.9 | — | 明确 | 高 | 合理 |
| HTF 融合 | 9.3 | +0.5 | 明确 | 高 | ~~衰减统一~~ → 策略级覆盖；~~无时效性~~ → _bar_time + staleness 2×TF 检查；warmup 回填 |
| 置信度管线 | 9.3 | — | 明确 | 高 | 合理 |
| Regime 系统 | 9.1 | — | 明确 | 高 | 合理 |
| 日内绩效追踪 (PerformanceTracker) | 9.0 | +0.3 | 明确 | 高 | ~~category 依赖字符串~~ → 枚举校验 |
| 交易执行 (TradeExecutor) | 9.0 | +1.0 | 明确 | 高 | ~~dead code~~ 已删；~~队列溢出~~ → backpressure 重试 + overflow 计数 |
| 执行门控 (ExecutionGate) | 8.5 | — | 明确 | 高 | 合理 |
| 风险管理 (Risk) | 9.0 | +0.5 | 明确 | 高 | ~~规则顺序无文档~~ → fast-fail 注释；规则错误码细化 |
| API 层 | 9.0 | +1.5 | 明确 | 高 | ~~错误码不完整~~ → 4 个新错误码；~~无版本化~~ → /v1/ 前缀；~~DI 上帝对象~~ → 4 子容器拆分 |
| 持久化 (Persistence) | 8.0 | — | 明确 | 高 | 通道不可动态修改 [P3 Backlog] |
| 仓储层 (Repositories) | 8.0 | — | 明确 | 高 | 查询接口有限 [P3 Backlog] |
| 监控 (Monitoring) | 8.5 | +1.0 | 明确 | 高 | ~~cache_hit_rate 告警不合理~~ → 降级为信息指标；新增 indicator_compute_p99_ms 告警 |
| 配置 (Config) | 8.5 | — | 明确 | 高 | 热加载不完整 [P3 Backlog] |
| 文档 (docs/) | 9.0 | +2.0 | — | — | ~~策略数量过期~~ → 已更新；~~缺少 PerformanceTracker~~ → 已补充；新增数据生产消费链路总图 |

### 2.2 模块职责定义

| 模块 | 核心职责 | 生产者 | 消费者 |
|------|---------|--------|--------|
| **BackgroundIngestor** | 从 MT5 拉取 tick/quote/ohlc/intrabar 数据 | MT5 API | MarketDataService, StorageWriter |
| **MarketDataService** | 线程安全的内存行情缓存，唯一运行时数据权威 | BackgroundIngestor | API 路由, IndicatorManager, SignalRuntime |
| **StorageWriter** | 异步多通道队列持久化到 TimescaleDB | Ingestor, IndicatorManager | TimescaleDB |
| **UnifiedIndicatorManager** | 事件驱动的指标编排器，管理 confirmed/intrabar 双链路 | MarketDataService 事件 | SignalRuntime（via 快照发布） |
| **SignalRuntime** | 信号主循环：过滤→评估→状态机→发布 | IndicatorManager 快照 | TradeExecutor, QualityTracker, HTFCache |
| **SignalModule** | 策略注册、evaluate 入口、置信度修正管线 | SignalRuntime 调用 | 策略实例 |
| **VotingEngine** | 多策略加权投票产生共识信号 | SignalRuntime 调用 | SignalRuntime 状态机 |
| **TradeExecutor** | 信号自动下单（异步队列 + daemon 线程） | SignalRuntime 信号事件 | TradingModule → MT5 |
| **ExecutionGate** | 策略域准入检查（group/whitelist/armed） | TradeExecutor 调用 | — |
| **PreTradeRiskService** | 多规则前置风控评估 | TradeExecutor 调用 | — |
| **PositionManager** | 持仓监控、止损跟踪、日终平仓 | TradeExecutor 通知 | TradeOutcomeTracker |
| **HTFStateCache** | 缓存高 TF 信号方向，供 HTF 对齐修正 | SignalRuntime 事件 | SignalRuntime 查询 |

### 2.3 解耦评估

**设计良好的解耦点**：
- 信号层与交易层：通过 `signal_listener` 回调解耦
- 指标层与信号层：通过 `UnifiedIndicatorSourceAdapter` 适配器解耦
- 策略与框架：通过 `SignalStrategy` Protocol 解耦
- HTF 注入：通过 INI 配置驱动，策略代码无 HTF 声明

**需要改进的耦合点**：
- `_Container`（deps.py）持有 18 个组件，是上帝对象
- `signal.ini` 承载 29+ sections，可按功能子域拆分
- `set_intrabar()` 同步 listener 调用将采集线程与指标计算耦合

---

## 三、数据 Scope 分叉全景

### 3.1 分叉点：BackgroundIngestor

```
BackgroundIngestor._run() 主循环
│
├─ _ingest_ohlc()  ──────────────────── Confirmed 链路
│   │ 节流：next_ohlc_at 时间戳（ohlc_interval 控制）
│   │ 仅写入已收盘 bar
│   ↓
│   MarketDataService.set_ohlc_closed()
│   + enqueue_ohlc_closed_event()
│   ↓
│   IndicatorManager._event_loop → process_closed_bar_events_batch()
│   ↓ 全部 21 个 enabled 指标
│   ↓ publish_snapshot(scope="confirmed")
│   ↓
│   SignalRuntime._confirmed_events（2048, 不可丢弃）
│
└─ _ingest_intrabar() ──────────────── Intrabar 链路
    │ 节流：next_intrabar_at（per-tf config > global > auto max(5s, tf×5%)）
    │ 获取当前未收盘 bar
    │ OHLC 去重：值与上次相同则跳过
    ↓
    MarketDataService.set_intrabar()
    ↓ 同步触发 listener → IndicatorManager._on_intrabar()
    ↓ _intrabar_queue（put_nowait, 满则丢弃）
    ↓ _intrabar_loop 独立线程（节流 1s min）
    ↓ 仅策略推导的 8 个 intrabar 指标
    ↓ store_preview_snapshot() 去重 → publish_snapshot(scope="intrabar")
    ↓
    SignalRuntime._intrabar_events（4096, 可丢弃, confirmed 优先消费）
```

### 3.2 指标计算 Scope 差异

| 维度 | Confirmed | Intrabar |
|------|-----------|----------|
| 触发源 | bar 收盘事件 | intrabar 轮询更新 |
| 指标数量 | 全部 21 个 enabled | 自动推导 ~8 个 |
| 数据输入 | N 根完整收盘 K 线 | N-1 收盘 + 当前未收盘 bar |
| 持久化 | 事件持久化到 SQLite events.db | 不持久化，best-effort |
| 队列策略 | 有 backpressure 重试 | put_nowait, 满则丢弃 |
| 去重 | 无（每次收盘都计算） | 快照相同则不发布 |

### 3.3 信号 Scope 差异

| 维度 | Confirmed | Intrabar |
|------|-----------|----------|
| 状态机 | confirmed_buy/sell/cancelled | preview → armed |
| 衰减 | 无 | 策略级覆盖 > 全局 ×0.85（strategy_params __intrabar_decay） |
| 持久化 | 写入 signal_records 表 | 写入 signal_records 表 |
| 交易执行 | TradeExecutor 仅处理 confirmed | 不直接执行（armed 状态预备） |
| HTF 对齐 | 全强度 | 半强度 ×0.5 |

---

## 四、按优先级分层的优化计划

### P0 — 必须立即修复（正确性/数据安全）✅ 全部完成

#### P0-1: Dead Code 清理 — `outcome_tracker.py`

| 项目 | 内容 |
|------|------|
| **问题** | `src/trading/outcome_tracker.py`（419 行）中的 `OutcomeTracker` 类完全未被任何模块导入。`__init__.py` 仅导出 `TradeOutcomeTracker`，grep 确认无外部引用。 |
| **影响** | 代码库混淆、维护成本、新开发者误用 |
| **修改文件** | 删除 `src/trading/outcome_tracker.py`；更新 `src/persistence/schema/signal_outcomes.py` 第 4 行注释（"OutcomeTracker" → "SignalQualityTracker"） |
| **验证** | `pytest` 全量通过，无 ImportError |

#### P0-2: Intrabar 缓存追加语义审查

| 项目 | 内容 |
|------|------|
| **问题** | `MarketDataService.set_intrabar()` 第 462 行：同一 `bar_time` 多次进入时 `existing.append(bar)`，累积历史快照而非覆盖当前值。有 `_intrabar_max_points` 上限保护，但语义需确认是否符合下游预期。 |
| **影响** | 同一 bar 产生多个快照累积在 `_intrabar_cache` 中 |
| **修改文件** | `src/market/service.py` 第 459-464 行 |
| **方案** | 若仅需当前 bar 最新值 → 改为 `existing[-1] = bar`（覆盖）；若需保留演化历史 → 添加注释明确语义 |
| **验证** | 新增 `tests/market/test_set_intrabar.py` 覆盖同 bar_time 重复写入 |

---

### P1 — 短期优化（可靠性/可维护性）✅ 全部完成

#### P1-1: Sizing D1 时间框架缺少配置

| 项目 | 内容 |
|------|------|
| **问题** | `src/trading/sizing.py` 第 9-23 行的 `TIMEFRAME_SL_TP` 和 `TIMEFRAME_RISK_MULTIPLIER` 仅覆盖 M1/M5/M15/H1，缺少 D1。D1 交易使用默认值（sl=1.5, tp=3.0），可能不适合日线级别波动。 |
| **修改文件** | `src/trading/sizing.py` |
| **方案** | 添加 `"D1": {"sl_atr_mult": 2.0, "tp_atr_mult": 4.0}` 和 `"D1": 1.50` |
| **验证** | `tests/trading/test_sizing.py` 新增 D1 解析测试 |

#### P1-2: BackgroundIngestor 退避阈值硬编码

| 项目 | 内容 |
|------|------|
| **问题** | `src/ingestion/ingestor.py` 第 59-64 行三个退避参数直接硬编码：`_SYMBOL_ERROR_THRESHOLD=5`, `_SYMBOL_COOLDOWN_SECONDS=60.0`, `_SYMBOL_MAX_COOLDOWN_SECONDS=300.0` |
| **修改文件** | `config/ingest.ini`（添加 `[error_recovery]`），`src/config/centralized.py`（IngestSettings 新增字段），`src/ingestion/ingestor.py`（从 settings 读取） |
| **验证** | 配置不同阈值运行测试 |

#### P1-3: HTF 对齐修正魔数配置化

| 项目 | 内容 |
|------|------|
| **问题** | `src/signals/orchestration/runtime.py` 第 1356-1364 行有 3 个硬编码常量：strength 系数 0.3、stability 每 bar 增幅 0.03 和上限 1.15、intrabar 半强度 0.5。注意 `base` 值（1.10/0.70）已从配置读取。 |
| **修改文件** | `config/signal.ini`（添加 `[htf_alignment]`），`src/config/models/signal.py`，`src/signals/orchestration/runtime.py` |
| **方案** | 新增配置：`strength_coefficient=0.3`, `stability_per_bar=0.03`, `stability_cap=1.15`, `intrabar_strength_ratio=0.5` |
| **验证** | 修改配置值后验证 HTF 修正结果变化 |

#### P1-4: TradeExecutor 队列溢出改进

| 项目 | 内容 |
|------|------|
| **问题** | `src/trading/signal_executor.py` 第 94 行队列 `maxsize=64`，第 129-133 行 `put_nowait()` 满则丢弃 confirmed 信号 |
| **修改文件** | `src/trading/signal_executor.py`，`config/signal.ini` |
| **方案** | ① 队列大小可配置；② confirmed 信号 `put(timeout=1.0)` 重试（参考 SignalRuntime backpressure 模式）；③ 添加溢出计数到执行统计 |
| **验证** | `tests/trading/test_executor_async.py` 队列满场景 |

#### P1-5: PositionManager 恢复失败日志级别

| 项目 | 内容 |
|------|------|
| **问题** | `src/trading/position_manager.py` 恢复失败仅 `logger.debug`，运维无法感知 |
| **修改文件** | `src/trading/position_manager.py` |
| **方案** | 改为 `logger.warning`，添加 recovered 失败计数到返回值 |
| **验证** | 模拟回调失败场景 |

#### P1-6: `_results` dict 添加容量限制

| 项目 | 内容 |
|------|------|
| **问题** | `src/indicators/manager.py` 第 114 行 `_results` 是普通 Dict，无淘汰机制。当前规模（105 条）安全，但缺乏长期运行保护。 |
| **修改文件** | `src/indicators/manager.py` |
| **方案** | 改为 `OrderedDict` + LRU 淘汰（上限 2000 条）；在 `get_performance_stats()` 中报告大小 |
| **验证** | 长期运行下 `_results` 大小监控 |

---

### P2 — 中期改进（工程化/可扩展性）✅ 全部完成

#### P2-1: 策略 category 枚举化

| 项目 | 内容 |
|------|------|
| **问题** | `category` 是自由字符串（"trend"/"reversion"/"breakout"/...），PerformanceTracker 依赖它做分类聚合，拼写错误导致聚合失效 |
| **修改文件** | `src/signals/strategies/base.py`（添加 `StrategyCategory` 枚举），`src/signals/service.py`（校验），各策略文件 |
| **方案** | `class StrategyCategory(str, Enum)` 包含所有已知 category；`_validate_strategy_attrs()` 校验 |
| **验证** | 启动时自动校验非法 category |

#### P2-2: API 风险错误码细化

| 项目 | 内容 |
|------|------|
| **问题** | `trade_dispatcher.py` 第 44 行大多数风险规则映射到通用 `TRADE_BLOCKED_BY_RISK`，API 客户端无法区分具体阻止原因 |
| **修改文件** | `src/api/error_codes.py`，`src/api/trade_dispatcher.py` |
| **方案** | 添加细化错误码：`MARGIN_INSUFFICIENT`, `TRADE_FREQUENCY_LIMITED`, `POSITION_LIMIT_REACHED` 等；按 `RiskCheckResult.rule_name` 映射 |
| **验证** | 触发不同风险规则返回不同错误码 |

#### P2-3: Intrabar 置信度衰减策略级差异化

| 项目 | 内容 |
|------|------|
| **问题** | 全局 `intrabar_confidence_decay=0.85`，但振荡器类（RSI/CCI）intrabar 值相对可靠，通道类（Boll）变化大 |
| **修改文件** | `config/signal.ini`（`[strategy_params]` 支持 `策略名__intrabar_decay`），`src/signals/orchestration/runtime.py` |
| **方案** | 按策略查找衰减系数，fallback 到全局值。推荐：振荡器 0.90，通道 0.80 |
| **验证** | 不同策略 intrabar confidence 差异化 |

#### P2-4: 风险规则执行顺序文档化

| 项目 | 内容 |
|------|------|
| **问题** | `PreTradeRiskService.rules` 的执行顺序影响 fast-fail 行为但无文档说明 |
| **修改文件** | `src/risk/service.py`（添加规则顺序注释） |
| **方案** | 在 rules 元组上方添加设计原则注释（快速检查优先：Account → DailyLoss → Margin → Frequency → Protection → Session → MarketStructure → Economic → Calendar） |
| **验证** | 代码审查 |

#### P2-5: `set_intrabar()` listener 超时防护

| 项目 | 内容 |
|------|------|
| **问题** | 当前 `IndicatorManager` 的 intrabar 回调已异步化（put_nowait），但 listener 机制本身缺乏防护——新增慢 listener 会阻塞采集线程 |
| **修改文件** | `src/market/service.py` 第 465-469 行 |
| **方案** | 添加 listener 调用超时监控日志（>100ms 记 warning）；或统一改为异步入队模式 |
| **验证** | 压力测试下监控采集延迟 |

#### P2-6: event_store SQLite 自动清理

| 项目 | 内容 |
|------|------|
| **问题** | `events.db` 无自动清理机制，依赖手工调用 `cleanup_old_events(days_to_keep=7)` |
| **修改文件** | `src/indicators/manager.py` |
| **方案** | 在 `_event_loop` 中添加每日一次自动清理（每 24h 执行 `cleanup_old_events(7)`） |
| **验证** | 长期运行下 events.db 大小稳定 |

---

### 已完成的清理

| 项目 | 状态 |
|------|:----:|
| Dead Code: `outcome_tracker.py` (419行) | ✅ 已删除 |
| `docs/architecture.md` 策略数量 14→18+2 | ✅ 已更新 |
| `docs/signal-system.md` 添加 performance.py | ✅ 已更新 |
| `signal_outcomes.py` 注释 OutcomeTracker→SignalQualityTracker | ✅ 已更新 |
| `CLAUDE.md` Known Issues 更新 | ✅ 已更新 |
| `docs/architecture-flow.md` 数据生产消费链路总图 | ✅ 已新增 |

---

## 六、实施进度

| 阶段 | 包含项 | 状态 | 关键修改 |
|------|-------|:----:|---------|
| **Sprint 1** | P0-1, P0-2, P1-1, P1-5 | ✅ 完成 | 删除 dead code(419行)、D1 sizing、日志级别修复、文档清理 |
| **Sprint 2** | P1-2, P1-3, P1-4, P1-6 | ✅ 完成 | 退避阈值配置化、HTF 对齐配置化、队列 backpressure、_results LRU |
| **Sprint 3** | P2-4, P2-5, P2-6 | ✅ 完成 | 风控规则文档化、SQLite 自动清理、listener 超时防护 |
| **Sprint 4** | P2-1, P2-2, P2-3, 文档 | ✅ 完成 | category 枚举化、风控错误码细化、intrabar 衰减差异化、数据流链路图 |
| **HTF 改进** | get_indicator bar_time + warmup + staleness | ✅ 完成 | _bar_time 元数据、30s warmup 回填、2×TF staleness 检查 |
| **P3 Backlog** | 按需排入 | 待实施 | 见下方 |

### P3 — 剩余 Backlog（按推荐优先级排序）

| 编号 | 项目 | 难度 | 影响 | 说明 |
|:---:|------|:---:|:---:|------|
| ~~P3-4~~ | ~~后台线程优雅关闭~~ | — | — | ✅ 经审查：daemon=True 是合理安全网（stop+join 已完整覆盖所有组件，finally 块保证退出清理）。不需修改 |
| ~~P3-1~~ | ~~API 版本化~~ | — | — | ✅ 完成：`/v1/` 前缀路由 + 无前缀向后兼容并存；`/health` 保持根路径 |
| ~~P3-2~~ | ~~DI 容器拆分~~ | — | — | ✅ 完成：4 个子容器（Market/Signal/Trading/Monitoring）+ property 代理向后兼容 |
| ~~P3-5~~ | ~~SmartCache TTL 按指标细分~~ | — | — | ✅ 完成：`IndicatorConfig.cache_ttl` 可选字段 + `SmartCache.set(ttl=)` per-key TTL |
| P3-3 | 配置热加载完善 | 高 | 中 | signal.ini FileWatcher + `/monitoring/reload-config` API |
| ~~P3-6~~ | ~~监控告警阈值校准~~ | — | — | ✅ 完成：`cache_hit_rate` 降级为信息指标；新增 `indicator_compute_p99_ms`（warning=500ms, critical=2000ms） |
| P3-7 | StorageWriter 通道动态管理 | 高 | 低 | 运行时通道注册/注销 + 自适应调整 |
| ~~P3-8~~ | ~~测试覆盖增强~~ | — | — | ✅ 完成：HTF staleness 3 例 + SmartCache per-key TTL 4 例 + category 校验 3 例 + compute_p99 告警 1 例 |

---

## 七、关键亮点（值得保留的优秀设计）

1. **事件驱动 + 双队列分离**：confirmed 优先消费，intrabar best-effort，防止高频事件饿死关键信号
2. **分片锁**：`_shard_locks[(symbol, tf)]` 按维度分片，避免全局锁争用
3. **SignalEvent frozen dataclass**：不可变设计，安全跨线程传递
4. **置信度四层修正链**：affinity × performance × calibrator × HTF，清晰可溯源
5. **Intrabar 指标自动推导**：策略 `preferred_scopes` + `required_indicators` 自动确定 intrabar 计算集合，无硬编码
6. **Regime 单次计算复用**：所有策略共享同一次 Regime 检测结果
7. **HTF 纯 INI 驱动**：策略代码零 HTF 声明，全部通过 `signal.ini [strategy_htf]` 配置
8. **TimeframeScaler**：按时间框架自动缩放策略参数，减少跨 TF 参数失配
9. **超时防护 + 指数退避**：BackgroundIngestor 对 MT5 API 冻结的优雅处理（参数已配置化）
10. **Protocol-based 策略框架**：鸭子类型 + 注册时验证（含 StrategyCategory 枚举校验），兼顾灵活性与安全性
11. **HTF staleness 检查**：`_bar_time` 元数据 + 2×TF 周期阈值自动跳过陈旧数据，策略零感知
12. **Warmup 机制**：启动后 30s 内加速 reconcile，消除高 TF 冷启动指标盲区
13. **TradeExecutor backpressure**：confirmed 信号队列满时 1s 重试而非立即丢弃 + overflow 计数器
14. **风控错误码分级**：9 条规则按 fast-fail 顺序执行，API 返回独立错误码（非通用 TRADE_BLOCKED_BY_RISK）
