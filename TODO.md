# TODO

按优先级和领域分组。每条注明影响模块和决策要点。

---

## 回测模块重构（下一 session 优先）

### P0: BacktestConfig 拆分为嵌套子配置

当前 BacktestConfig 有 64 个字段平铺在一个 dataclass 中，违反单一职责。

- 拆分为 ~8 个子 dataclass：`PositionConfig` / `FilterConfig` / `RiskConfig` / `PendingEntryConfig` / `TrailingTPConfig` / `CircuitBreakerConfig` / `MonteCarloConfig` / `ConfidenceConfig`
- 影响范围：54+ 处 `config.filter_xxx` → `config.filters.xxx` 引用更新
- 涉及文件：`models.py` / `engine.py` / `engine_signals.py` / `engine_filters.py` / `paper_trading.py` / `cli.py` / `api_config.py` / 全部测试
- 执行方式：自底向上，先改 models.py，再逐文件更新引用

### P1: 硬编码魔数配置化

以下参数应从 `backtest.ini` 加载而非硬编码在构造函数默认值中：

| 参数 | 当前位置 | 当前默认值 | 建议 INI section |
|------|---------|-----------|-----------------|
| `htf_alignment_boost` | engine.py:119 | 1.10 | `[confidence]` |
| `htf_conflict_penalty` | engine.py:120 | 0.70 | `[confidence]` |
| `bars_to_evaluate` | engine.py:270 | 5 | `[persistence]` |

### P1: 补全回测模块缺失测试

| 文件 | 当前覆盖 | 需要补充 |
|------|---------|---------|
| `filters.py` / `engine_filters.py` | 无独立测试 | `test_backtest_filters.py` |
| `component_factory.py` | 仅集成测试 | `test_component_factory.py`（单元级） |
| `config.py` | 无独立测试 | `test_backtest_config.py`（INI 解析） |

---

## 回测精调（需要实操）

以下项需要实际运行回测 + 分析结果 + 调 INI 参数：

- supertrend / macd_momentum per-TF affinity 精调
- min_confidence 提到 0.60-0.65（A/B 对比）
- SL/TP ATR 倍数精调 + Trailing 参数调优
- Pending Entry 实盘效果验证
- 利用 strategy_weights 机制，回测计算投票组内策略相关性矩阵，对相关性 > 0.8 的策略设置降权

---

## API 层优化

### 响应格式统一

~15 个端点直接返回 Pydantic model 而非 `ApiResponse[T]` 包装。主要集中在：

- `monitoring_routes/`：health/live、health/ready、components、config/reload 等
- `studio_routes/`：agents、events、summary 缺少 response_model
- K8s 探针（/health/live, /health/ready）可例外

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
