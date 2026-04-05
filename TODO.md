# TODO

按优先级和领域分组。每条注明影响模块和决策要点。

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
- ~~持久化架构审查：health_monitor.db 重构为内存环形缓冲（3.3GB→0），SQLite 仅存告警历史~~
- ~~TimescaleDB retention + compression policy：16 表三级保留策略，启动时自动幂等配置~~
- ~~日志文件持久化：RotatingFileHandler 100MB×10 + WARNING 独立 errors.log~~
- ~~signal_preview_events 异步化：同步直写 PG → StorageWriter 异步队列（drop_newest）~~
- ~~配置隐私分层：signal.ini/risk.ini 私域值置空，凭据默认值清除，新建 risk.local.ini~~
- ~~统一 SQLite 连接工厂：3 处 _make_conn() → src/utils/sqlite_conn.py~~
- ~~flaky 集成测试修复：test_app_health 全局状态污染 + Event 驱动等待替代 polling~~
- ~~过时测试修复：rsi5 enabled 断言 + tick schema migration 断言~~

---

## P0: 回测精调（下一步优先）

以下项需要实际运行回测 + 分析结果 + 调 INI 参数：

- [ ] 各 TF 基线回测（M5/M15/M30/H1），建立当前策略体系的 PnL/WR/Sharpe 基线
- [ ] macd_divergence + adx_trend_fade 新策略回测验证（reversal_vote 组信号质量）
- [ ] supertrend / macd_momentum per-TF affinity 精调
- [ ] min_confidence 提到 0.60-0.65（A/B 对比当前 0.55 基线）
- [ ] SL/TP ATR 倍数精调 + Trailing TP 参数调优
- [ ] strategy_weights 相关性降权（投票组内相关性 > 0.8 的策略降权）
- [ ] Pending Entry 实盘效果验证

---

## P1: Paper Trading 验证

- [ ] config/paper_trading.ini 设 `enabled = true`
- [ ] OBSERVE 模式下运行一段时间，验证信号接收→模拟交易→持久化完整链路
- [ ] 对比 Paper Trading 绩效与回测预期

---

## P2: 架构演进（实盘稳定后再推进）

### A5: 挂单管理解耦

PendingOrderManager 从 TradeExecutor 内部提升为独立组件，通过 Port 交互。

- 影响：重构 TradeExecutor 构造函数和挂单提交/追踪逻辑
- 前提：当前耦合在实盘中是否造成测试困难

### A2: 事件溯源替代命令式写入

关键交易操作改为 Event Sourcing（event_store → projections），完整审计链 + 回放能力。

### A6: 品种级配置（多品种扩展前提）

lot_step / point_value / min_volume 等从 MT5 symbol_info 动态获取。

---

## P3: 前端 Dashboard

- [ ] 设计新 dashboard 架构（技术栈选型）
- [ ] 实现核心监控页面（行情/信号/持仓/绩效）
- [ ] 接入后端 API（交易 trace / SL TP 历史 / 投票详情 / 绩效追踪 / 相关性分析）
- [ ] Studio 模块暂保留，新 dashboard 不依赖 Studio 的 agent 模型
