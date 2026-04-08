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

---

## P1: Paper Trading 验证（已启用，等待积累数据）

> 目标：在不冒资金风险的情况下验证系统端到端工作。

- [x] `config/paper_trading.ini` 设 `enabled = true`，参数对齐回测
- [ ] OBSERVE 模式运行 1-2 周，验证完整链路：信号接收 → 模拟执行 → 持仓管理 → 持久化
- [ ] 对比 Paper Trading 实际绩效 vs 回测预期（关注：成交率、滑点假设、信号延迟）
- [ ] 如果 Paper 结果与回测差距 >30%，排查原因（过拟合？执行假设不成立？）
- 判定标准：Paper 结果确认后才可进入 P2 实盘阶段
- 注意：当前频率 ~1.5 笔/周，2 周可能仅积累 3-4 笔

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

---

## P5: 前端 Dashboard

- [ ] 技术栈选型（React/Vue/Svelte + 实时更新方案）
- [ ] 核心监控页面（行情/信号/持仓/绩效）
- [ ] 接入后端 API（交易 trace / SL TP 历史 / 投票详情 / 绩效追踪 / 相关性分析）
- [ ] Studio 模块暂保留，新 dashboard 不依赖 Studio 的 agent 模型
