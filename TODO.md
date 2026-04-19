# TODO

按优先级分组。核心原则：**先验证再优化再上线**。

---

## 📍 2026-04-19 状态快照（新 session 先读此节）

### 今日产出

**上一 session（分支 `fix/backtest-htf-required-indicators`，PR #48 已合入 main）**：

| commit | 内容 |
|--------|------|
| `840ae74` | docs(research): 2026-04-18 挖掘 vs 回测 Gap 系统分析 |
| `84dd9b8` | **fix(backtest)**: `_required_indicators` 聚合丢失 `htf_requirements` 导致 HTF alignment 策略 0 交易 |
| `42c37bb` | docs(backtest): 2026-04-19 baseline 验证 + P2/P3 策略诊断 + trend_continuation 冻结 |
| `eae0ab8` | docs(todo): 新 session 前置状态快照 |
| `63454b2` | Merge PR #48 |

**本 session（待提交）**：

| 内容 | 说明 |
|------|------|
| **fix(signals)**: `breakout_follow` 对 `adx_d3=None` 硬拒 | 诊断 P5 参数调优时发现：回测中 `adx_d3` 恒 None（`src/indicators/` 未实现 delta metric），`breakout_follow._why:62` 用 `if d3 is None or ...` 硬拒 → 100% 拦截。与 strong_trend_follow / session_breakout / regime_exhaustion 对齐 `is not None and` 模式修复 |

### 新 H1 Baseline（bugfix 后，12 个月 2025-04-17~2026-04-15，全策略）

| 维度 | bug 存在时 | **bugfix 后** | 变化 |
|------|---------|---------|------|
| Trades | 201 | **473** | +135% |
| WR | 44.3% | 46.1% | +1.8 pp |
| PF | 2.595 | 2.361 | -0.23（breakout_follow solo PF 1.23 拉低） |
| Sharpe | 2.168 | **2.74** | **+0.57** |
| Calmar | 5.845 | 21.77 | compound 放大 |
| MaxDD | 11.69% | **8.0%** | **-3.7 pp** |
| MaxDD duration | — | 75 bars | — |

策略分布（新 baseline）：
| 策略 | n | WR | PnL（$10k→$95k compound） |
|------|---|-----|------|
| regime_exhaustion | 52 | 57.7% | +$47,391 |
| strong_trend_follow | 62 | 50.0% | +$25,549 |
| **breakout_follow** | **163** | 44.2% | +$6,491 |
| pullback_window | 46 | 41.3% | +$3,136 |
| open_range_breakout | 72 | 40.3% | +$2,167 |
| trendline_touch | 78 | 47.4% | +$365 |

> PnL $85k vs 旧 $4,575 差异来自 compound：bugfix 新增 163 笔正收益 → balance 更快膨胀 → 后续 regime_exhaustion / strong_trend_follow 每笔 position_size 按 `current_balance × risk_pct / stop_distance` 放大。per-trade `pnl_pct` 与旧 baseline 基本一致（每笔 ±1%）。

### signal.local.ini 本机 override（不入 git）

```ini
[regime_affinity.structured_trend_continuation]
trending = 0.0
ranging = 0.0
breakout = 0.0
uncertain = 0.0
```

冻结原因：solo min_conf=0.10 时 33 trades PF 1.22；solo min_conf=0.45 时 **16 trades PF 0.61** —— 高 raw_confidence 反而 WR 更低，置信度校准反向。保留代码资产，未来可重设计后解冻。

### 🔴 新增未决工作

#### P5：breakout_follow 参数调优（可选，低优先级）

**状态**：原"参数调优"目标已被 bugfix 替代大部分效果（从 0 trades → 163 trades / PF 1.23）。

**仍可做**（非阻塞）：
- [ ] 如需让 breakout_follow solo PF 从 1.23 → 1.3+（减轻对整体 PF 的拖累），可做参数网格。ADX 参数覆盖机制已验证可用（`strategy_params_per_tf` 通过 `build_backtest_components` 传入）。
- [ ] 原 TODO 网格维度（`_di_diff_min` / `_momentum_consensus_buy_min` / `_rsi_max_buy` / `_rsi_min_sell`）可扫。
- [ ] 注意：breakout_follow 的 `_adx_d3_min=1.0` 门槛在回测**不生效**（None 放行），在生产**生效**——参数调优结果在 live 行为可能更严。

**前置**：建议先 1-2 周 Paper Trading 观察，对比回测 vs 实盘差异，再决定是否调优。

#### P6：trend_continuation 置信度重设计（长，跨多 session）

**问题**：`raw_confidence = base + why×0.15 + when×0.15 + where×0.10 + vol×0.05` 与实盘胜率**负相关**——高 confidence 反而触发过拟合场景。

**候选方向**：
- [ ] 分析哪些 (regime, direction) 组合的高 confidence 对应低 WR
- [ ] 考虑加入 **in-sample confidence 再校准**（基于历史 bucket 的 calibration）
- [ ] 或重写 Why/When/Where 评分逻辑（当前 pullback setup 设计可能本身过拟合）
- [ ] 目标：solo min_conf=0.45 时 PF > 1.2（当前 0.61）

#### P7：回测管线缺失 delta metrics（架构性，新增）

**问题**：`adx_d3` / `rsi_d3` 等三阶 delta metric 在 `src/indicators/` **完全未实现**。所有依赖它们的策略（至少 7 处：breakout_follow / strong_trend_follow / session_breakout / regime_exhaustion / sweep_reversal / range_reversion / trend_continuation / pullback_window / lowbar_entry）在回测中相关门控条件**被默默跳过**（通过 `is not None` 短路放行）。

**影响**：生产 vs 回测存在系统性行为差异，具体影响幅度**未量化**。

**候选方向**：
- [ ] 方案 A：在 `src/indicators/core/` 补 `adx_d3` / `rsi_d3` 增量计算（前值缓存 + 滚动差分）
- [ ] 方案 B：在文档里显式声明"回测跳过 delta 条件"，承认行为差异
- [ ] 方案 C：为 delta metric 提供回测专用降级近似（例如用 ADX 的 EMA 做 proxy）

### ⚠️ 旧 TODO 条目**已过期或证伪**

新 session 读到下列条目时**请忽略或以本节为准**：

| 位置 | 过期原因 |
|------|---------|
| Line 85-95 "回测基线（2026-04-08 3 个月，合计 19 笔）" | 已由本节 473 笔（12 个月，bugfix 后）取代 |
| "breakout_follow 零交易——设计如此" | **二次证伪**：不仅是 HTF infra bug（2026-04-19 上午已修），还有 adx_d3=None 硬拒 bug（下午已修） |
| "用 M5 bar 数据回测高 TF 策略盘中入场" | 应用 bugfix 后新 baseline 重跑 |

### 📝 补记：挖掘驱动策略的现有状态

- `regime_exhaustion`（已部署 paper_only）— **新 baseline 主要盈利贡献**：52 trades / 57.7% WR / +$47,391（compound 后）。**建议 Paper 观察独立跟踪**。
- `strong_trend_follow`（FP.2，已部署 paper_only）— 新 baseline 次主要贡献：62 trades / 50% WR / +$25,549。

---

## 已完成归档（2026-04-05）

- ~~P0: BacktestConfig 拆分为 8 个嵌套子配置 + `from_flat()` 向后兼容~~
- ~~P0: 回测子包化（5 个子包：engine/filtering/analysis/optimization/data）+ API 迁移到 `src/api/backtest*`~~
- ~~P1: 硬编码魔数配置化（htf_alignment_boost/conflict_penalty/bars_to_evaluate → ConfidenceConfig + backtest.ini）~~
- ~~P1: 补全缺失测试（+24 用例：test_backtest_filters/test_backtest_config/test_component_factory）~~
- ~~API 响应格式统一（15 个端点 → ApiResponse[T] 包装）~~
- ~~Paper Trading 模块：完整子包（6 文件 + 独立 DB 表 + API + Studio agent）~~
- ~~A4: 信号队列持久化（WalSignalQueue，SQLite WAL 模式）~~
- ~~A3: 增量指标扩展（9→15 incremental）+ 孤儿指标禁用（21→15 启用）~~
- ~~策略优化：+macd_divergence/adx_trend_fade，清理 squeeze_release，复合策略 5→4~~
- ~~数据库 schema 全面重建：26 表，14 hypertable，统一索引命名，CHECK 约束，+circuit_breaker_history~~
- ~~持久化架构审查：health_monitor.db → 内存环形缓冲（3.3GB→0）~~
- ~~TimescaleDB retention + compression policy：16 表三级保留策略~~
- ~~日志文件持久化：RotatingFileHandler 100MB×10 + WARNING 独立 errors.log~~
- ~~signal_preview_events 异步化：同步直写 PG → StorageWriter 异步队列~~
- ~~配置隐私分层：signal.ini/risk.ini 私域值置空，新建 risk.local.ini~~
- ~~统一 SQLite 连接工厂 + flaky 测试修复 + 过时测试修复~~
- ~~文档架构重组：CLAUDE.md 1710→248 行，docs/ 11→5 个文件~~

## 已完成归档（2026-04-06）

**P0 策略验证闭环**
- ~~P0.1: 各 TF 基线回测 → 全部亏损，确认问题在出场结构~~
- ~~P0.2: 策略相关性分析 + 裁剪 + 投票组重组~~
- ~~P0.3: 新增 5 个 legacy 策略（range_box_breakout/bar_momentum_surge/atr_regime_shift/swing_structure_break/range_mean_reversion）~~
- ~~P1.5: 出场参数配置化（Chandelier Exit aggression α 驱动）~~

**路径 B 策略体系架构重构**
- ~~结构化策略框架：StructuredStrategyBase + Where(软)/When(硬)/Why(硬) 三层评估~~
- ~~6 个结构化策略：TrendContinuation/SweepReversal/BreakoutFollow/RangeReversion/SessionBreakout/TrendlineTouch~~
- ~~策略目录子包化：structured/（活跃）+ legacy/（冻结/待定）~~
- ~~Volume 指标启用：vwap30/obv30/mfi14 + 新增 volume_ratio20~~
- ~~回测基础设施修复：MarketStructure 注入 + recent_bars 注入 + HTF fallback~~
- ~~信号质量优化 Step 2：冻结 4 个亏损策略 + regime 门控收紧 + reversion trending=0.0~~
- ~~计算优化：regime fast-reject（affinity<0.10 跳过三层评估）+ signal_grade A/B/C 自动计算~~

**长期运行稳定性修复**
- ~~EOD 跨日自动恢复 + apply_mode 异常保护 + TradeExecutor 双线程防护~~
- ~~WAL Queue 重入 + IndicatorManager 线程守卫 + PositionManager 启动同步~~
- ~~DLQ 文件清理 + listener_fail_counts 清理~~

**架构清理**
- ~~Legacy 策略全面移除：35 个 legacy 策略 + 4 个复合策略代码删除~~
- ~~切换到纯结构化策略架构：7 个策略（8 个注册实例含 trend_h4 变体）~~
- ~~清理 signal.ini：移除 legacy strategy_timeframes/regime_affinity/strategy_params/voting_groups~~
- ~~清理 composites.json / registry.py / composite.py~~

**A7 入场职责回归策略层**
- ~~策略 _entry_spec() 输出入场意图（entry_type/entry_price/entry_zone_atr）~~
- ~~PendingEntryManager 退化为纯执行层：读取策略入场规格，不再按 category 推算~~
- ~~移除 _CATEGORY_ZONE_MODE / _compute_reference_price() / category_timeout_multiplier~~
- ~~TradeExecutor 分支简化：entry_type=market → 直接市价，limit/stop → 挂单~~
- ~~signal.ini [pending_entry] 移除 zone ATR factors + per-TF 覆盖~~

**Regime 预检清理**
- ~~移除三层冗余 regime affinity 预检（affinity.py 全局预检 + evaluator 逐策略预检 + base.py fast-reject）~~
- ~~结构化策略在 _why() 内部自行判断 regime 适应性，外部预检纯属双重计算~~
- ~~删除 affinity.py、min_affinity_skip 配置、_regime_fast_reject 硬编码~~

**策略信号管线精简（A9 + 死代码清理 + HTF 冲突移除）**
- ~~htf_alignment 完全移除（不再计算/记录/乘 confidence）~~
- ~~Executor HTF 冲突检查移除（执行层不做信号质量决策）~~
- ~~死代码清理：confidence.py apply_htf_alignment / htf_resolver.py compute_htf_alignment~~

**统一评分框架 + 策略接口标准化**
- ~~每层 score 0~1 × budget（why=0.15, when=0.15, where=0.10, vol=0.05）~~
- ~~策略必须实现 6 个接口：_why / _when / _entry_spec / _exit_spec（强制）+ _where / _volume_bonus（可选）~~
- ~~出场参数（aggression + sl_atr + tp_atr）由策略 _exit_spec() 输出，执行层读取~~
- ~~checks.py 通用检查工具函数集（HTF/ADX/RSI/Bar/Volume 纯函数）~~

---

## P0: 结构化策略优化（当前最高优先）

> 目标：刷新纯结构化架构下的回测基线，优化各策略/TF/Regime 组合的盈利能力。
> 前置：Legacy 已移除 + A7 入场职责已回归策略层，管线已清洁。

### 回测基线（2026-04-08 最终，3 个月 2025-12-30~2026-03-30）

M15 已冻结。M30/H1 活跃。session_breakout/lowbar_entry 已冻结。

| TF | 笔数 | WR | PnL | PF | Sharpe | MaxDD | MC p | 策略分布 |
|----|------|-----|------|-----|--------|-------|------|---------|
| M30 | 16 | 68.8% | +271.20 | 2.47 | 0.814 | 6.63% | 0.057 | trend_cont+162, sweep+85, range+24 |
| H1 | 3 | 100% | +276.51 | ∞ | 0.854 | 4.59% | - | trendline_touch only |
| **合计** | **19** | **73.7%** | **+547.71** | - | - | - | - | |

vs 2026-04-07 原始基线：M30 39笔/-573 → 16笔/+271；H1 32笔/+1 → 3笔/+277。

### 已完成（2026-04-08）

- [x] **trend_continuation M30 修复**：HTF ADX min 18→20，RSI buy [32,55]→[30,50] sell [45,68]→[50,70] 消除重叠，breakout/uncertain affinity 0.50/0.25→0.20/0.10。23笔/-163 → 7笔/+162
- [x] **trendline_touch H1 breakout 过滤**：breakout affinity 0.50→0.20，uncertain 0.40→0.25。8笔/+0.03 → 3笔/+277
- [x] **aggression 全量网格搜索（M30+H1）**：sweep_reversal 0.80→0.60，range_reversion 0.15→0.20，H1 trendline_touch 移除 0.10 覆盖恢复代码默认 0.70
- [x] **lowbar_entry 确认冻结**：所有 aggression 值均亏损，入场质量不足
- [x] **session_breakout 冻结**：M30 全 aggression 0% WR，入场质量不足
- [x] **Paper Trading 配置**：`paper_trading.ini` 启用，参数对齐回测（balance=2000, commission=7, slippage=5）

### 待观察

- [ ] M30 Monte Carlo p=0.057（接近 0.05 边界但未达显著），需更长回测期或更多交易确认
- [ ] breakout_follow M30/H1 零交易——条件严格，不产生信号（设计如此）
- [ ] 频率瓶颈：19 笔/3 个月（~1.5 笔/周），需通过新增策略或放宽条件提频

### Telegram 通知模块 Phase 1（2026-04-17 完成）

> 插拔式、模板化、分级过滤的运行时事件推送。详见 `docs/design/notifications.md`。

- [x] Pydantic 配置模型 + 分层 ini 加载器（`src/config/{models/,}notifications.py`）
- [x] 核心类型：`NotificationEvent` / `Severity` / `build_dedup_key`
- [x] 轻量模板引擎（`{{ var }}` + `{% if %}`，不依赖 Jinja2）+ 启动期严格校验
- [x] 5 CRITICAL + 3 WARNING + 1 INFO 起步模板
- [x] `Deduper`（LRU + TTL）+ `RateLimiter`（令牌桶 global + per-chat）
- [x] `OutboxStore`（L2 SQLite + 指数退避 + DLQ + 崩溃恢复）
- [x] `TelegramTransport`（requests + 识别 429 `retry_after` + 5xx 可重试 / 4xx 终态 + proxy）
- [x] `PipelineEventClassifier`（6 个事件 mapper + 异常隔离不杀 listener）
- [x] `NotificationDispatcher` + worker 线程（CRITICAL 限流不丢 + ADR-005 防双线程）
- [x] `NotificationModule` 组装根（stop 不关 outbox 支持 toggle + close 终态清理）
- [x] DI 接入（container/factory/builder_phases/builder/runtime/deps，Phase 5.5）
- [x] `HealthMonitor.set_alert_listener()` 钩子（~15 行最小侵入）
- [x] `/v1/admin/notifications/{status,toggle}` API
- [x] 206 条测试（单元 + 模板 linter + 实 HealthMonitor hook 集成）

### FP.2 — 第 2 个挖掘驱动策略（2026-04-17 完成）

基于 FP.1 长窗口挖掘（12 个月）H1 rule_mining #5：
`adx>40.12 AND macd_fast.hist<=1.61 AND roc12.roc>-1.17 → buy`（test 60.1%/n=143）

- [x] 设计 + 实现 `structured_strong_trend_follow`（与 regime_exhaustion 在 adx_d3 互斥）
- [x] 12 个月回测 Round 0（`_adx_extreme=40`）：Sharpe 1.446（差 0.054）未达 1.5 最低线
- [x] C2 Round 1（`_adx_extreme=38` 放宽，signal.local.ini 覆盖）：**Sharpe 1.546 达标**
      全量：Trades 64 / WR 46.9% / PnL +873 / PF 2.15 / MaxDD 6.97% / MC p=0.019
- [x] `deployment = paper_only` 部署（locked_timeframes=H1，locked_sessions=london,new_york）
- [ ] Paper 观察 1-2 周，对照 12 个月回测：关注触发频率、WR 偏差、SL/TP 分布
- [ ] 考虑绑定到 live_exec_a（与 regime_exhaustion 同账户，形成"耗竭反转 + 强势延续"互补对）

---

## P1: Paper Trading 验证（已启用，等待积累数据）

> 目标：在不冒资金风险的情况下验证系统端到端工作。

- [x] `config/paper_trading.ini` 设 `enabled = true`，参数对齐回测
- [x] **修复 `TrackedPosition.initial_risk` 永不初始化 P0 bug**（2026-04-14 codebase-review #83）
      Chandelier Exit 运行时此前完全失效，修复前 Paper 数据不可信。
- [x] **Chandelier/exit_profile 配置拆出到 config/exit.ini，支持实例级覆盖**（#84）
- [x] **配置双实例角色分工**（2026-04-14）：
      - `live-main`（保守）：trendline_touch + trend_continuation + trend_h4；
        紧 trail（trend_trending=0.65）、早 breakeven（0.05R）、超时保护（48 bars）、max_tp_r=4.0
      - `live-exec-a`（稍激进）：sweep_reversal + range_reversion + breakout_follow
        + trend_h4_momentum + session_breakout/lowbar_entry（冻结观测）；
        宽 trail（trend_trending=0.90）、宽 breakeven（0.15R）、无超时、max_tp_r=6.0
      - `auto_trade_enabled = true`（策略 deployment 仍是 paper_only，live 仍被阻断）
- [ ] OBSERVE 模式运行 1-2 周，验证完整链路：信号接收 → 模拟执行 → 持仓管理 → 持久化
- [ ] 对比 Paper Trading 实际绩效 vs 回测预期（关注：成交率、滑点假设、信号延迟）
- [ ] 分别观察 live-main 与 live-exec-a 的出场行为：SL 是否真的在移动、两边 aggression 差异是否体现
- [ ] 如果 Paper 结果与回测差距 >30%，排查原因（过拟合？执行假设不成立？）
- 判定标准：Paper 结果确认后才可进入 P2 实盘阶段
- 注意：multi-account 分工后预期频率 ~5-7 笔/周（main 低频 trend + worker-a 高频反转/突破）

---

## P2: 参数优化（Paper Trading 验证通过后）

### 2.1 Walk-Forward 前推验证

- [ ] 对结构化策略跑 Walk-Forward（3 个月 IS + 1 个月 OOS，滚动 4-6 窗口）
- [ ] 检查 overfitting_ratio：>1.5 的策略标记为不可信
- [ ] 检查 consistency_rate：<50% 的策略参数不稳定
- [ ] 用 `RecommendationEngine.generate()` 生成调参建议

### 2.2 Per-TF 参数精调

- [ ] 结构化策略 RSI 区间 / ADX 阈值 per-TF 网格搜索
- [ ] SL/TP ATR 倍数精调（特别是 M30 的 trailing stop 参数）
- [ ] min_confidence per-TF 调整（当前 M30=0.42, H1=0.40）
- 所有调参结果写入 `signal.local.ini`，不修改 `signal.ini`

---

## P3: 实盘试运行（参数优化完成后）

- [ ] 最小手数（0.01）+ 最保守风控参数
- [ ] `risk.local.ini` 设置：`daily_loss_limit_pct = 3.0`、`max_volume_per_order = 0.01`
- [ ] FULL 模式运行 1-2 周，每日检查：
  - 信号质量（`/signals/monitoring/quality/{symbol}/{timeframe}`，例如 `/signals/monitoring/quality/XAUUSD/M30`）
  - 交易审计（`/trade/command-audits`）
  - 持仓状态（`/account/positions`）
  - 日志文件（`data/logs/errors.log`）
- [ ] 确认无异常后逐步放大手数

---

## P4: 架构演进（实盘稳定后）

### 配置热重载

- [ ] signal.ini 交易成本阈值偏紧（base_spread_points=30, max_spread_to_stop_ratio=0.33）启动持续警告
- [ ] `[regime_detector]` / `[strategy_params]` 热重载支持（当前需重启）
- [ ] market.ini 应用壳层配置热重载决策

### Intrabar 交易链路（已实现，2026-04 重构完成）

> IntrabarTradeCoordinator 独立负责 bar 计数稳定性判定。
> 连续 N 根同方向子 TF bar 达标时直接发布 `intrabar_armed` 信号触发盘中入场。
> 旧的 preview/armed 状态机和 require_armed 门控已移除，coordinator 信号独立发布。

- [x] bar 内去重机制：IntrabarTradeGuard（同 bar/策略/方向只允许入场一次）
- [x] confirmed 协调：盘中已开仓 → confirmed 同向 skip / 反向放行
- [x] 盘中入场条件：coordinator min_stable_bars + min_confidence 阈值
- [x] 风控覆盖：复用完整 pre-trade filter chain
- [ ] 仓位计算：盘中 ATR 不完整，需用上一根完整 bar 的 ATR 或低 TF ATR
- [ ] 回测支持：用 M5 bar 数据回测高 TF 策略的盘中入场效果

### Intrabar 工程化后续步骤（2026-04-09 新增）

> 目标：把 intrabar 从“best-effort 可用”提升到“可量化、可降级、可审计”的交易级链路。

- [ ] **SLO 与告警基线**
  - [ ] 定义并落地 3 个核心 SLO：`intrabar_drop_rate_1m`、`intrabar_queue_age_p95`、`intrabar_to_decision_latency_p95`
  - [ ] 在 `/signals/monitoring` 或健康端点暴露 intrabar 溢出统计（wait_success / replace_success / overflow_failures）
  - [ ] 设定分级告警阈值（INFO/WARN/CRITICAL）并写入运行文档

- [ ] **调度策略从固定配额升级为自适应配额**
  - [ ] 将 confirmed:intrabar 固定让路策略（当前 burst 限额）改为基于 backlog/queue_age 的动态配额
  - [ ] 增加“高拥塞模式”下的自动限流与恢复条件，避免 intrabar 长尾延迟放大
  - [ ] 补充回放测试：低波动 / 高波动 / 极端爆量 3 组场景对比

- [ ] **链路降级矩阵（Degrade Ladder）**
  - [ ] 定义 L0-L3 四档降级：全量 intrabar → 白名单策略 → 核心 TF → 仅 confirmed
  - [ ] 建立降级触发器（queue_age、drop_rate、CPU 负载）与回升条件
  - [ ] 将当前“随机丢弃”转为“可预期降级”，并在状态接口中可见

- [ ] **启动前置校验与运行态自愈**
  - [ ] intrabar_trading=true 时，启动阶段校验 listener/策略白名单/trigger 映射完整性
  - [ ] 当出现“无 listener 跳过”持续超阈值时触发自愈动作（降级或切回 confirmed-only）
  - [ ] 补充 runbook：排查步骤、指标看板、回滚开关

- [ ] **端到端可审计 Trace**
  - [ ] 统一 trace_id 贯通：`intrabar_bar -> indicator_snapshot -> strategy_decision -> vote -> gate -> order`
  - [ ] 在 trade audit 视图增加“本次下单来自哪个 intrabar 快照”的可追踪字段
  - [ ] 增加 1 条失败链路示例（被 filter/gate 阻断）用于回归测试与演示

**前置条件**：P0-P1 完成，confirmed 链路策略有效性已验证

### 结构化策略架构适配（路径 B 后续）

> 策略变聪明后，管线应变简单。以下改动消除策略层与管线层的职责重叠。
> 横切关注点（performance/calibrator）通过**装饰器**接入，不在各模块重复实现。

#### ~~A7: 入场职责回归策略层~~（已完成 2026-04-06）

#### A8: PerformanceTracker 按信号等级追踪（装饰器接入）

- [ ] 回测验证结构化策略有效后，通过装饰器接入 PerformanceTracker
- [ ] PerformanceTracker key 从 `strategy_name` 改为 `(strategy_name, signal_grade)`
- [ ] 乘数按 grade 分别计算：A 级信号可获更高 multiplier，C 级被压制
- [ ] signal_grade A/B/C 已在 StructuredStrategyBase.evaluate() 中自动计算

#### A9: 置信度管线精简（已完成 2026-04-06）

- [x] **htf_alignment 完全移除**：不再计算、不再记录；结构化策略 Why 层内部处理 HTF 方向
- [x] **Executor HTF 冲突检查移除**：执行层不做信号质量决策
- [x] **死代码清理**：confidence.py apply_htf_alignment / htf_resolver.py compute_htf_alignment 移除
- [x] **Calibrator/PerformanceTracker**：配置层不启用，管线透传
- [x] **统一评分框架**：每层 score 0~1 × budget（why=0.15, when=0.15, where=0.10, vol=0.05）

#### ~~A10: 策略工具函数集~~（已完成 2026-04-06）

- [x] `checks.py` 纯函数工具集：htf_direction / adx_in_range / rsi_extreme / bar_close_position 等
- [x] 策略按需调用，不强制使用

#### A11: regime_affinity 乘数审视（待回测数据验证）

> _why() 已用 ADX/HTF 做了 regime 检查（硬门控），regime_affinity 在 service.py 中再乘一次（软门控）。
> 两者对同一件事做了两次判断，可能需要移除或简化。需要对比回测数据决定。

- [ ] 对比 regime_affinity=1.0（禁用）vs 当前值的回测差异
- [ ] 如果差异不大，移除 regime_affinity 乘法，管线简化为纯策略评分

### 架构解耦

- [ ] A5: PendingOrderManager 从 TradeExecutor 提升为独立组件
- [ ] A2: 关键交易操作改为 Event Sourcing

### Telegram 通知模块 Phase 2-5

> Phase 1 已完成（见归档）。后续按优先级逐步展开。

#### Phase 2：定时任务 + 零侵入适配器（2026-04-17 完成）

- [x] **NotificationScheduler** `src/notifications/scheduler.py`：DailyAt + Interval job，1 秒 tick，ADR-005 合规
- [x] **TradingStateAlerts 轮询适配**：60s 轮询 `summary()`，3 种 code 映射（pending_missing → critical / orphan+active_mismatch → warn / unmanaged → critical），未知 code 降级
- [x] **outbox 清理定时器**：`purge_sent_older_than(7 days)` 每 6h 跑一次
- [x] **daily_report 调度占位**：scheduler 注册，Phase 2.5 注入内容生成器
- [x] **NotificationModule 集成**：`scheduler_running` + `scheduler_jobs` 进 status()

#### Phase 2.5：daily_report 内容 + 运维工具（待办）

- [ ] **daily_report 内容生成器**：复用 RuntimeReadModel 组装 health/positions/executor 摘要 → `info_daily_report` 模板
- [ ] **INFO 模板补齐**：`info_daily_report`、`info_mode_changed`、`info_startup_ready`、`info_position_closed`
- [ ] **CLI 测试工具**：`python -m src.ops.cli.test_notification --event <type>` 支持本地触发样本事件验证部署

#### Phase 3：入站查询命令

- [ ] `TelegramPoller`（long polling getUpdates，独立线程）
- [ ] `CommandRouter` + chat_id allowlist + 命令白名单
- [ ] 查询 handlers（调用 `RuntimeReadModel`，只读）：
  - `/health` → 3 行摘要
  - `/positions` → 仓位一览（symbol/direction/R/unrealized_pnl）
  - `/signals` → 信号引擎 running + queue depth + drop rate
  - `/executor` → enabled + circuit_open + 今日统计
  - `/pending` → 待执行信号列表
  - `/mode` → FULL/OBSERVE/RISK_OFF/INGEST_ONLY 当前状态
  - `/daily-report` → 手动触发日报

#### Phase 4：入站控制命令（仅低风险）

- [ ] `/halt [reason]` / `/resume [reason]` → `OperatorCommandService.enqueue(command_type=set_trade_control)`
- [ ] `/reload-config` → `reload_configs()`
- [ ] 审计链：actor=`telegram:{username}:{chat_id}`，回执含 `action_id` + `audit_id`
- [ ] 入站速率限制（`inbound_per_chat_per_minute`）防命令轰炸

#### Phase 5（可选）

- [ ] `/trace <signal_id>` 深度链路（多段分块，处理 4096 字节限制）
- [ ] `/v1/admin/notifications/dlq` DLQ 查询端点
- [ ] `/v1/admin/notifications/reload-templates` 模板热重载
- [ ] `/v1/admin/notifications/test` 手动触发事件端点

---

## P5: 前端 Dashboard

- [ ] 技术栈选型（React/Vue/Svelte + 实时更新方案）
- [ ] 总览控制塔读模型补齐（为 QuantX 首页提供一等交易指导数据）
  - [ ] 账户级资金快照：`balance / equity / free_margin / margin / margin_level / floating_pnl / daily_realized_pnl / currency / updated_at`
  - [ ] 组合级资金聚合：`total_equity / total_free_margin / total_margin / accounts_with_funding / margin_blocked_accounts`
  - [ ] 多币种聚合语义：明确 `base_currency` 或 `fx_normalized_totals`，否则返回 `aggregation_mode = mixed_currency`，避免前端误把多账户资金直接求和
  - [ ] 当前交易条件摘要：`risk_action / risk_reason / primary_blocker / margin_check_details / order_protection_requirements`
  - [ ] 总览焦点账户：`focus_account_alias / focus_account_label / focus_basis(active|default|selected)`，避免前端自行猜测“当前该看哪个账户”
  - [ ] 数据节奏元信息：`recommended_poll_interval_seconds / supports_stream / last_quote_at / last_account_risk_at / last_trade_state_at`
  - [ ] 模块级刷新语义：区分 `capital_snapshot` 与 `analysis_context` 的 cadence，避免资金快照受时间窗切换影响
  - [ ] 行情快照原生读模型：`symbol / bid / ask / spread / quote_updated_at / quote_freshness_state / recommended_poll_interval_seconds`，避免首页继续直接消费裸 `/v1/quote`
  - [ ] 建议下沉为原生总览读模型，避免前端再从 `trade/accounts + trade/state + entry_status` 拼装
- [ ] 核心监控页面（行情/信号/持仓/绩效）
- [ ] 接入后端 API（交易 trace / SL TP 历史 / 投票详情 / 绩效追踪 / 相关性分析）
- [ ] Studio 模块暂保留，新 dashboard 不依赖 Studio 的 agent 模型
