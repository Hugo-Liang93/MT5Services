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

---

## P0: 结构化策略优化（当前最高优先）

> 目标：让结构化策略在 H1 trending regime 上的盈利能力（WR=48.1%, PnL=+85）扩展到更多场景。

### 回测现状（2026-04-06，3 个月 2025-12-30~2026-03-30）

| 策略 | H1 | M30 |
|------|-----|------|
| structured_trend_continuation | 18笔/44.4%/-61 | 32笔/40.6%/-316 |
| structured_sweep_reversal | — | 2笔/100%/+91 |
| structured_trendline_touch | 9笔/33.3%/-362 | — |
| macd_momentum (legacy) | 28笔/42.9%/+3 | — |
| **H1 TRENDING regime** | **52笔/48.1%/+85** | — |

### 待优化

- [ ] H1 BREAKOUT regime 拖累修复（34笔/29.4%/-1013）— 结构化策略在 breakout 下入场时机差
- [ ] structured_trend_continuation RSI 区间网格搜索（当前 32-55 buy / 45-68 sell）
- [ ] structured_trendline_touch WR=33.3% 分析 — 趋势线质量评估是否需要更严格
- [ ] M30 整体优化：W/L=0.80，trailing_stop 盈利但 SL 亏损过大
- [ ] 冻结 M30 上的 order_block_entry（28笔/25%/-814，最大亏损源）

### P0.4 Legacy 策略 Confidence 优化（降级为可选）

> 路径 B 已用结构化策略替代，以下仅在需要保留 legacy 策略活跃时执行。

- [ ] sma_trend / macd_momentum / roc_momentum：用百分位排名替代绝对值映射
- [ ] 可选：HTF alignment 改加减法替代乘法

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

- [ ] 对结构化策略 + macd_momentum，跑 Walk-Forward（3 个月 IS + 1 个月 OOS，滚动 4-6 窗口）
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

#### A7: 入场职责回归策略层

- [ ] 策略 evaluate() 在 metadata 中输出 `suggested_entry_price` + `entry_type`（limit/market）
- [ ] PendingEntryManager 退化为纯执行层：收到策略建议价 → 挂单 → 超时取消
- [ ] 废弃 PendingEntryManager 的 "按 category 选入场模式" 逻辑（pullback/momentum/symmetric 模板）
- [ ] TradeExecutor 读取 metadata 决定挂单/市价，不再自行推算入场价

#### A8: PerformanceTracker 按信号等级追踪（装饰器接入）

- [ ] 回测验证结构化策略有效后，通过装饰器接入 PerformanceTracker
- [ ] PerformanceTracker key 从 `strategy_name` 改为 `(strategy_name, signal_grade)`
- [ ] 乘数按 grade 分别计算：A 级信号可获更高 multiplier，C 级被压制
- [ ] signal_grade A/B/C 已在 StructuredStrategyBase.evaluate() 中自动计算

#### A9: 置信度管线精简（结构化策略专用）

- [ ] **移除 htf_alignment**：结构化策略 Why 层已做 HTF 方向检查，外部再乘 ×1.10/×0.70 是双重计算
- [ ] **简化 Calibrator**：结构化策略的 confidence 设计已考虑分布（base + 多因子加分），短期跳过校准
- [ ] **保留 regime_affinity**：外部 regime 门控仍有价值，但值可放宽（策略已内部检查 ADX/趋势方向）
- [ ] **保留 confidence_floor + intrabar_factor**
- [ ] 实现方式：按 strategy category 判断是否跳过 htf_alignment / calibrator

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
