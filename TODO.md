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

### 0.2 策略相关性分析 + 裁剪（部分完成 2026-04-06）

- [x] H1 相关性矩阵：ema_ribbon×hma_cross r=1.0, momentum_vote×supertrend r=1.0
- [x] 确认已有冻结策略（6 个 affinity=0.0）有效
- [x] Confidence 管线根因分析：低 conf 来自 affinity 压制 + 乘法链衰减
- [ ] momentum_vote 投票组重组（当前与 supertrend 100% 相关，退化为单策略）
- [ ] 基于新出场体系重新评估已冻结策略的 affinity 值
- 结论：H1 上实际只有 2 个独立信号源（supertrend 系），需要引入新维度策略

### 0.3 新策略引入（进行中 2026-04-06）

已完成：
- [x] `range_box_breakout`（箱体突破）→ H1: 8 笔 WR=50% PnL=+15 **唯一盈利策略**
- [x] `bar_momentum_surge`（大 bar 动量）→ H1: 30 笔 WR=43%
- [x] `bar_stats20` 指标（bar body/range 滚动统计）

待完成：
- [ ] `bar_momentum_surge` per-TF 参数调优（M30 WR=14% → body_multiplier 需提到 2.5+）
- [ ] ATR Regime Shift 策略（波动率压缩→扩张）
- [ ] Swing Structure 策略（高低点结构 + 回调入场）
- [ ] Trend Exhaustion Filter（趋势衰竭过滤器，作为 SignalFilterChain 组件）
- [ ] Range Mean Reversion 策略（箱体内回归，填补 ranging 覆盖）

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
