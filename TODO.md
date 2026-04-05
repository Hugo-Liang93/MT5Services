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

### 0.1 各 TF 基线回测

- [ ] M5 基线回测（近 3 个月数据）→ 记录 PnL / WR / Sharpe / MaxDD / 总交易数
- [ ] M15 基线回测 → 同上
- [ ] M30 基线回测 → 同上（此前已有初步结果，需用最新参数重跑）
- [ ] H1 基线回测 → 同上
- [ ] 汇总对比表：哪些 TF 盈利、哪些亏损、哪些样本不足
- 工具：`tools/backtest_runner.py` 本地直接执行
- 产出：每个 TF 的基线数据写入回测结果表，作为后续所有优化的参照基准

### 0.2 策略相关性分析 + 裁剪

- [ ] 用 `correlation.py` 跑各 TF 的策略信号方向相关性矩阵
- [ ] 识别高相关策略对（>0.7）：这些策略同时入场等于变相加仓
- [ ] 相关组内保留 Sharpe 最高的，其余通过 regime_affinity 全设 0.0 禁用
- [ ] 投票组成员重新评估：组内不应有高度相关的策略
- 目标：从 31 个基础策略精简到 15-20 个真正独立的信号源

### 0.3 新策略验证

- [ ] macd_divergence 各 TF 回测 → 判断是否值得保留在 reversal_vote 组
- [ ] adx_trend_fade 各 TF 回测 → 同上
- [ ] trendline_3touch 回测验证（该策略目前完全未经验证）

---

## P1: Paper Trading 验证（基线建立后）

> 目标：在不冒资金风险的情况下验证系统端到端工作。回测 ≠ 实盘，Paper Trading 是桥梁。

- [ ] `config/paper_trading.ini` 设 `enabled = true`
- [ ] OBSERVE 模式运行 1-2 周，验证完整链路：信号接收 → 模拟执行 → 持仓管理 → 持久化
- [ ] 对比 Paper Trading 实际绩效 vs 回测预期（关注：成交率、滑点假设、信号延迟）
- [ ] 如果 Paper 结果与回测差距 >30%，排查原因（过拟合？执行假设不成立？）
- 判定标准：Paper 结果确认后才可进入 P2 实盘阶段

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
