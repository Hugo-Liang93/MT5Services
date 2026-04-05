# TODO

按优先级和领域分组。每条注明影响模块和决策要点。

---

## 已完成归档（2026-04-05）

- ~~P0: BacktestConfig 拆分为 8 个嵌套子配置 + `from_flat()` 向后兼容~~
- ~~P0: 回测子包化（5 个子包：engine/filtering/analysis/optimization/data）+ API 迁移到 `src/api/backtest*`~~
- ~~P1: 硬编码魔数配置化（htf_alignment_boost/conflict_penalty/bars_to_evaluate → ConfidenceConfig + backtest.ini）~~
- ~~P1: 补全缺失测试（+24 用例：test_backtest_filters/test_backtest_config/test_component_factory）~~
- ~~API 响应格式统一（15 个端点 → ApiResponse[T] 包装）~~

---

## Paper Trading 模块（下一 session 优先）

从 `src/backtesting/paper_trading.py` 半成品升级为 backtesting 子包，作为回测→实盘的信任桥梁。

### 设计方案

**定位**：用实盘信号流 + 实时行情做"影子交易"，产生独立的策略评估数据。不经过 MT5 执行，不影响实盘任何数据。

**模块结构**（backtesting 子包，职责属于回测→验证流程）：
```
src/backtesting/paper_trading/
├── __init__.py
├── bridge.py          # PaperTradingBridge：信号监听 + 模拟执行
├── portfolio.py       # 持仓管理（复用 engine/portfolio.py 或独立适配）
├── tracker.py         # PaperTradeTracker：持久化钩子（写入独立表）
├── config.py          # PaperTradingConfig 加载（config/paper_trading.ini）
├── models.py          # PaperTradeRecord / PaperSignalEvaluation 数据类
└── ports.py           # Protocol 定义（与 SignalRuntime / MarketDataService 交互边界）
```

**数据表设计（独立于实盘表，不混表存储）**：
```
paper_trade_outcomes     ← 模拟交易记录（独立表，不与 trade_outcomes 混用）
paper_signal_evaluations ← 模拟信号评估（独立表，不与 signal_outcomes 混用）
paper_trading_sessions   ← 模拟交易 session（start/stop/config snapshot/汇总指标）
```

**运行时集成**：
- 注册为 `RuntimeManagedComponent`，`supported_modes = {FULL, OBSERVE}`
- OBSERVE 模式下自动激活（TradeExecutor 静默时 Paper Trader 接管）
- 作为 SignalRuntime 的 signal listener（和 TradeExecutor 并列）

**绩效反馈**：
- 独立的 `PaperPerformanceTracker`（不混入实盘 StrategyPerformanceTracker）
- 通过 API 暴露 paper trading 专属绩效（胜率/Sharpe/盈亏/持仓）
- 与实盘绩效并排对比的 ReadModel

**配置**：`config/paper_trading.ini`（独立于 backtest.ini）

**API**：在 `src/api/backtest_routes/` 中新增 paper trading 端点，或单独建 `src/api/paper_trading_routes/`

**监控**：Studio 新增 `paper_trader` agent 角色

### 关键准则
- 数据表完全独立于实盘表，不混表存储
- 模块间通过 Protocol/Port 交互
- 所有参数通过 INI 配置驱动

### 前置清理
- 删除 `src/backtesting/paper_trading.py`（半成品，有缩进 bug，未集成）
- 在 `src/backtesting/paper_trading/` 下全新实现为子包，不做兼容

---

## 回测精调（需要实操）

以下项需要实际运行回测 + 分析结果 + 调 INI 参数：

- supertrend / macd_momentum per-TF affinity 精调
- min_confidence 提到 0.60-0.65（A/B 对比）
- SL/TP ATR 倍数精调 + Trailing 参数调优
- Pending Entry 实盘效果验证
- 利用 strategy_weights 机制，回测计算投票组内策略相关性矩阵，对相关性 > 0.8 的策略设置降权

---

## 架构演进（实盘稳定后再推进）

### A4: 信号队列持久化

confirmed_events 队列改为 WAL-backed（当前内存队列重启丢失），重启后信号不丢失。

- 影响：需要用 SQLite WAL 替换 `queue.Queue`，改动 SignalRuntime 的事件入队/出队逻辑
- 前提：先验证实盘重启频率和信号丢失是否真正是问题

### A5: 挂单管理解耦

PendingOrderManager 从 TradeExecutor 内部提升为独立组件，通过 Port 交互。

- 影响：重构 TradeExecutor 构造函数和挂单提交/追踪逻辑
- 前提：当前耦合在实盘中是否造成测试困难

### A2: 事件溯源替代命令式写入

关键交易操作改为 Event Sourcing（event_store → projections），完整审计链 + 回放能力。

### A3: 指标增量计算扩展

将更多指标从 `standard` → `incremental` compute_mode，减少 confirmed 全量重算。多品种扩展时的计算时间优化。

### A6: 品种级配置

lot_step / point_value / min_volume 等从 MT5 symbol_info 动态获取，多品种扩展前提。

---

## 前端规划

- 计划创建全新 dashboard 监控前端（Anteater 项目已弃用）
- 后端 API 已完备（交易 trace / SL TP 历史 / 投票详情 / 绩效追踪 / 相关性分析）
- Studio 模块暂保留，新 dashboard 不依赖 Studio 的 agent 模型
