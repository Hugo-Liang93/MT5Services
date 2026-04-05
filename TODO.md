# TODO

按优先级和领域分组。每条注明影响模块和决策要点。

---

## 已完成归档（2026-04-05）

- ~~P0: BacktestConfig 拆分为 8 个嵌套子配置 + `from_flat()` 向后兼容~~
- ~~P0: 回测子包化（5 个子包：engine/filtering/analysis/optimization/data）+ API 迁移到 `src/api/backtest*`~~
- ~~P1: 硬编码魔数配置化（htf_alignment_boost/conflict_penalty/bars_to_evaluate → ConfidenceConfig + backtest.ini）~~
- ~~P1: 补全缺失测试（+24 用例：test_backtest_filters/test_backtest_config/test_component_factory）~~
- ~~API 响应格式统一（15 个端点 → ApiResponse[T] 包装）~~
- ~~Paper Trading 模块：从半成品升级为完整子包（6 个文件 + 独立 DB 表 + API 端点 + Studio agent）~~
- ~~A4: 信号队列持久化 — confirmed_events 改为 SQLite WAL-backed（WalSignalQueue），重启后信号不丢失~~
- ~~A3: 指标增量计算扩展 — 新增 6 个增量指标（MACD/ADX/Supertrend/Keltner/Donchian），从 9→15 个 incremental~~
- ~~禁用 6 个孤儿指标（ema21/ema55/rsi5/macd_fast/stoch14/williamsr14），启用指标从 21→15~~
- ~~清理 squeeze_release（禁用策略 + 从投票组移除 + 从 2 个复合策略移除，复合策略 5→4）~~
- ~~新增 macd_divergence 策略（MACD 柱状图背离检测）+ adx_trend_fade 策略（ADX 趋势衰竭），加入 reversal_vote~~
- ~~数据库 schema 全面重建：21 个 DDL，14 个 hypertable，统一索引命名，CHECK 约束，删除遗留 trade_operations，新增 circuit_breaker_history~~

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

### ~~A4: 信号队列持久化~~ ✓

~~已完成：`WalSignalQueue` (`src/signals/orchestration/wal_queue.py`)，SQLite WAL 模式。~~

### A5: 挂单管理解耦

PendingOrderManager 从 TradeExecutor 内部提升为独立组件，通过 Port 交互。

- 影响：重构 TradeExecutor 构造函数和挂单提交/追踪逻辑
- 前提：当前耦合在实盘中是否造成测试困难

### A2: 事件溯源替代命令式写入

关键交易操作改为 Event Sourcing（event_store → projections），完整审计链 + 回放能力。

### ~~A3: 指标增量计算扩展~~ ✓

~~已完成：MACD/MACD_fast/ADX/Supertrend/Keltner/Donchian 6 个指标转为增量模式。~~
~~剩余 standard：stoch14/williamsr14/roc12/cci20/stoch_rsi14/hma20（窗口型/多层计算，增量收益低）。~~

### A6: 品种级配置

lot_step / point_value / min_volume 等从 MT5 symbol_info 动态获取，多品种扩展前提。

---

## 前端规划

- 计划创建全新 dashboard 监控前端（Anteater 项目已弃用）
- 后端 API 已完备（交易 trace / SL TP 历史 / 投票详情 / 绩效追踪 / 相关性分析）
- Studio 模块暂保留，新 dashboard 不依赖 Studio 的 agent 模型
