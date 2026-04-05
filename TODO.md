# TODO

按优先级和领域分组。每条注明影响模块和决策要点。

---

## 回测精调（需要实操）

以下项需要实际运行回测 + 分析结果 + 调 INI 参数，不是代码架构变更：

- supertrend / macd_momentum per-TF affinity 精调
- min_confidence 提到 0.60-0.65（A/B 对比）
- SL/TP ATR 倍数精调 + Trailing 参数调优
- 增强 `tools/backtest_runner.py` 支持命令行参数覆盖
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
