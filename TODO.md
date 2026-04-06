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

---

## P0: 策略验证闭环（最高优先 — 上线前必须完成）

> 目标：用数据证明策略体系有效，而不是靠直觉。没有基线无法判断任何调参是"改进"还是"过拟合"。

### 0.1 各 TF 基线回测 ✅ 已完成（2026-04-06）

- [x] M5/M15/M30/H1 基线回测（旧出场体系）→ 全部亏损
- [x] H1 零成本验证 → WR=72.5%, Sharpe=1.03 → **策略方向判断有效**
- [x] SL/TP 网格搜索 → 36 组合，确认固定 ATR 倍数模式无法解决盈亏比问题
- [x] Chandelier Exit 出场系统重构 → regime-aware + 梯度锁利
- 结论：问题在出场结构（盈亏比 W/L=0.4），不在策略方向。已切换到 Chandelier Exit
- 下一步：出场参数配置化 → 回测验证 → 策略裁剪

### 0.2 策略相关性分析 + 裁剪 ✅ 已完成（2026-04-06）

- [x] H1 相关性矩阵：ema_ribbon×hma_cross r=1.0, momentum_vote×supertrend r=1.0
- [x] 确认已有冻结策略（6 个 affinity=0.0）有效
- [x] Confidence 管线根因分析：低 conf 来自 affinity 压制 + 乘法链衰减
- [x] momentum_vote 投票组重组：supertrend+bar_momentum_surge+swing_structure_break（3 个不同维度）
- [x] breakout_vote 加入 atr_regime_shift + range_box_breakout；reversion_vote 加入 range_mean_reversion
- [x] donchian_breakout/roc_momentum/order_block_entry 回归 standalone 独立评估
- [x] squeeze_release 从 breakout_vote 移除（回测 0% 有效信号）
- [x] 6 个冻结策略重评估：冻结原因为信号冗余/数据缺陷，与出场体系无关 → 全部维持 affinity=0.0

### 0.3 新策略引入 ✅ 已完成（2026-04-06）

- [x] `range_box_breakout`（箱体突破）→ H1: 8 笔 WR=50% PnL=+15 **唯一盈利策略**
- [x] `bar_momentum_surge`（大 bar 动量）→ H1: 30 笔 WR=43%
- [x] `bar_stats20` 指标（bar body/range 滚动统计）
- [x] `bar_momentum_surge` per-TF 参数配置化（M30: body_multiplier=2.5, close_thresh=0.75）
- [x] `atr_regime_shift`（波动率压缩→扩张，ADX delta + DI 方向 + BB 压缩）
- [x] `swing_structure_break`（Donchian 突破 + ADX 趋势确认 + RSI 过滤）
- [x] `range_mean_reversion`（BB 通道位置回归 + ADX 低位 + RSI 一致性，填补 ranging 覆盖）
- [x] `TrendExhaustionFilter`（SignalFilterChain warn 型组件：ADX 从高位下降 = 趋势衰竭警告）
- 下一步：各 TF 回测验证新策略效果

### 0.4 策略 Confidence 公式优化

- [ ] sma_trend / macd_momentum / roc_momentum：用百分位排名替代绝对值映射
- [ ] 目标：raw_conf 分布从 [0.40, 0.60] 拓宽到 [0.40, 0.90]
- [ ] 可选：HTF alignment 改加减法替代乘法（减少管线乘法链衰减）

---

## P1: Paper Trading 验证（基线建立后）

> 目标：在不冒资金风险的情况下验证系统端到端工作。回测 ≠ 实盘，Paper Trading 是桥梁。

- [ ] `config/paper_trading.ini` 设 `enabled = true`
- [ ] OBSERVE 模式运行 1-2 周，验证完整链路：信号接收 → 模拟执行 → 持仓管理 → 持久化
- [ ] 对比 Paper Trading 实际绩效 vs 回测预期（关注：成交率、滑点假设、信号延迟）
- [ ] 如果 Paper 结果与回测差距 >30%，排查原因（过拟合？执行假设不成立？）
- 判定标准：Paper 结果确认后才可进入 P2 实盘阶段

---

## P1.5: 出场参数配置化 ✅ 已完成（2026-04-06）

- [x] signal.ini 新增 `[chandelier]` + `[exit_profile]` + `[exit_profile.tf_scale]`
- [x] aggression 系数驱动 profile（α → mult/lock/breakeven_r，36→12 参数）
- [x] per-TF trail 缩放（M5:×1.20 → D1:×0.90）
- [x] R 单位保护约束（chandelier_mult ≥ initial_SL_mult）
- [x] 删除死参数 lock_step_r（连续锁利，无阶梯跳变）
- [x] 实盘监控修复：bars_held 动态计算 + active_positions Chandelier 状态 + status 配置可观测
- [ ] H1 回测验证新出场系统（对比旧体系基线）
- [ ] 确认 W/L ≥ 1.0 + WR ≥ 45% 的可行参数区间

---

## P2: 参数优化（Paper Trading 验证通过后）

### 2.1 Walk-Forward 前推验证

- [ ] 对基线回测 Sharpe 前 5 的策略，跑 Walk-Forward（3 个月 IS + 1 个月 OOS，滚动 4-6 窗口）
- [ ] 检查 overfitting_ratio：>1.5 的策略标记为不可信
- [ ] 检查 consistency_rate：<50% 的策略参数不稳定
- [ ] 用 `RecommendationEngine.generate()` 生成调参建议

### 2.2 Per-TF 参数精调

- [ ] min_confidence 提到 0.60-0.65（A/B 对比当前 0.55 基线）
- [ ] SL/TP ATR 倍数精调（特别是 M5/M15 的短周期，当前可能过大）
- [ ] Trailing TP 参数调优（activation_atr / trail_atr）
- [ ] supertrend / macd_momentum per-TF affinity 精调
- [ ] Pending Entry 各 TF 的 ATR 区间参数验证
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

## 已完成归档（2026-04-06 长期运行稳定性修复）

- [x] EOD 跨日自动恢复：`resolve_session_start()` 新交易日检测 + `_is_auto_transitioned` 标志
- [x] P0: `apply_mode()` 异常保护——组件部分失败仍更新模式，不再半启动半停止
- [x] P0: TradeExecutor 双线程防护——stop 超时保留引用 + start 等待僵尸 + _start_worker 存活检查
- [x] P1: WAL Queue `reopen()` 方法——close 后重入执行 _init_db + _closed 标志检查
- [x] P1: IndicatorManager 全线程守卫——`_any_thread_alive()` + stop 清引用 + 超时警告
- [x] P1: PositionManager 启动时立即 reconcile——修复 stop 期间 peak_price 断档
- [x] P2: DLQ 文件 7 天自动清理——`_cleanup_stale_dlq()` 在启动时执行
- [x] P2: `_listener_fail_counts` 清理——`remove_signal_listener()` 同步 pop

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

#### A7: 入场职责回归策略层

- [ ] 策略 evaluate() 在 metadata 中输出 `suggested_entry_price` + `entry_type`（limit/market）
- [ ] PendingEntryManager 退化为纯执行层：收到策略建议价 → 挂单 → 超时取消
- [ ] 废弃 PendingEntryManager 的 "按 category 选入场模式" 逻辑（pullback/momentum/symmetric 模板）
- [ ] TradeExecutor 读取 metadata 决定挂单/市价，不再自行推算入场价

#### A8: PerformanceTracker 按信号等级追踪

- [ ] 策略 evaluate() 在 metadata 输出 `signal_grade`（A/B/C：三层都满足=A，两层=B，仅硬门控=C）
- [ ] StructuredStrategyBase.evaluate() 自动计算 grade（基于 where_bonus + vol_bonus 是否 > 0）
- [ ] PerformanceTracker key 从 `strategy_name` 改为 `(strategy_name, signal_grade)`
- [ ] 乘数按 grade 分别计算：A 级信号可获更高 multiplier，C 级被压制

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
