# TODO

按优先级分组。核心原则：**先验证再优化再上线**。

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

### 回测基线（2026-04-07，3 个月 2025-12-30~2026-03-30）

M15 已冻结（PF=0.00）。仅 M30/H1 活跃。

| TF | 笔数 | PnL | PF | 主要问题 |
|----|------|-----|-----|---------|
| H1 | 32 | +1 | 1.00 | lowbar_entry ranging 29笔/-160 拖累 |
| M30 | 39 | -573 | 0.41 | trend_continuation breakout/uncertain 全亏 |

关键发现：出场全部由 Chandelier trailing（aggression）驱动，初始 SL/TP 几乎不影响结果。

### 待办

- [ ] **aggression 网格搜索**：per-strategy × per-TF 搜索最优 α（当前核心调参变量）
- [ ] **lowbar_entry 决策**：收紧 ADX 门控（36→22 仅 ranging）或冻结
- [ ] **trend_continuation M30 修复**：breakout+uncertain 21笔全亏，_why() 条件需收紧
- [ ] structured_trendline_touch WR 分析 — 趋势线质量评估是否需要更严格

---

## P1: Paper Trading 验证（结构化策略回测通过后）

> 目标：在不冒资金风险的情况下验证系统端到端工作。

- [ ] `config/paper_trading.ini` 设 `enabled = true`
- [ ] OBSERVE 模式运行 1-2 周，验证完整链路：信号接收 → 模拟执行 → 持仓管理 → 持久化
- [ ] 对比 Paper Trading 实际绩效 vs 回测预期（关注：成交率、滑点假设、信号延迟）
- [ ] 如果 Paper 结果与回测差距 >30%，排查原因（过拟合？执行假设不成立？）
- 判定标准：Paper 结果确认后才可进入 P2 实盘阶段

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
  - 信号质量（`/signals/monitoring/quality`）
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

### Intrabar 交易链路（从观测层升级为交易层）

> 当前 intrabar 仅做 preview/armed 预热，`require_armed=false`，对交易无实际影响。
> 目标：让 intrabar 策略能盘中直接入场，获得更好的入场价格。

**核心设计**：用低 TF 完整 K 线替代当前 OHLC 快照作为盘中观测源

```
H1 策略盘中入场示例：
  M5 bar 收盘（L1 可靠）→ 计算 M5 指标（语义正确的完整 K 线指标）
    → 作为 H1 策略的盘中输入 → 策略评估
    → 信号稳定 ≥ N 根 M5 bar → 盘中入场
    → H1 bar 收盘 → confirmed 验证：一致→持仓继续，反转→平仓
```

- [ ] 设计文档：`docs/design/intrabar-trading.md`（链路、去重、风控、回测方案）
- [ ] bar 内去重机制：同一 bar 内同一策略同一方向只允许入场一次（`traded_this_bar`）
- [ ] confirmed 验证角色：intrabar 已开仓 → confirmed 反转时平仓（而非重复开仓）
- [ ] 盘中入场条件：独立策略模式（不经投票组）+ 更高 min_confidence 阈值
- [ ] 仓位计算：盘中 ATR 不完整，需用上一根完整 bar 的 ATR 或低 TF ATR
- [ ] 回测支持：用 M5 bar 数据回测高 TF 策略的盘中入场效果
- [ ] 风控覆盖：intrabar 交易需经过同样的风控堆栈（可精简但不可跳过）

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
- [ ] A6: 品种级配置（多品种扩展前提）

---

## P5: 前端 Dashboard

- [ ] 技术栈选型（React/Vue/Svelte + 实时更新方案）
- [ ] 核心监控页面（行情/信号/持仓/绩效）
- [ ] 接入后端 API（交易 trace / SL TP 历史 / 投票详情 / 绩效追踪 / 相关性分析）
- [ ] Studio 模块暂保留，新 dashboard 不依赖 Studio 的 agent 模型
