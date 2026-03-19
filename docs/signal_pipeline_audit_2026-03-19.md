# Signal 链路审计（入口 -> Signal 调度 -> 风控 -> 执行）

## 1) 当前链路梳理（事实）

1. **服务入口**：`app.py` 读取 API 配置，启动 `uvicorn src.api:app`。  
2. **依赖容器初始化**：`src/api/deps.py::_ensure_initialized()` 组装 MarketDataService / StorageWriter / IndicatorManager / EconomicCalendarService / TradingModule / SignalModule / SignalRuntime。  
3. **快照触发**：`SignalRuntime._on_snapshot()` 接收指标快照并按 scope（confirmed/intrabar）入队。  
4. **信号过滤**：`SignalFilterChain` 在 runtime 消费事件时参与 session/spread/economic 过滤。  
5. **策略评估**：`SignalModule.evaluate()` 执行策略 + regime affinity + calibrator，并持久化信号。  
6. **订单域风控**：最终下单前由 `src.risk` / `PreTradeRiskService` 做账户与订单级别风险校验。  

---

## 2) 发现的问题（按优先级）

### P0（建议尽快）

### 2.1 初始化并发安全风险
`_ensure_initialized()` 目前没有显式互斥锁。若某些边界场景下多请求并发触发初始化，理论上存在重复构造组件风险（尤其是线程/监听器重复注册）。  
**建议**：在 `_ensure_initialized()` 外层增加全局初始化锁 + 双重检查。  

### 2.2 API 直接访问 runtime 私有字段
`/signals/regime/...` 路由读取 `runtime._regime_trackers`（私有属性）。这会导致 API 与内部实现强耦合，后续重构容易破接口。  
**建议**：在 runtime 暴露 `get_regime_stability(symbol,timeframe)` 公共方法，路由只调用公开接口。  

---

### P1（中期）

### 2.3 confirmed 队列仍可能丢事件
虽然 confirmed/intrabar 已分队列，但 confirmed 队列满时仍会 drop（只是单独计数）。对于“不可丢失”的收盘事件，仍有业务风险。  
**建议**：
- confirmed 改为 block/backpressure（或写入持久化缓冲）
- intrabar 保持可丢弃

### 2.4 Session 命名不统一
过滤器会输出 `newyork`，诊断/日报使用 `new_york`，存在观测维度不一致风险。  
**建议**：统一 session enum（asia/london/new_york/off_hours），全链路复用同一常量。

### 2.5 质量日报依赖时间字段质量
日报 24h 过滤依赖 `bar_time/generate_at` 可解析；若上游字段异常会被静默跳过，导致 `rows_analyzed` 偏低。  
**建议**：增加 `skipped_rows_invalid_time` 计数并输出到日报。

---

### P2（优化）

### 2.6 缺少链路级 trace_id
当前从 snapshot -> decision -> persistence -> executor 没有统一 trace_id，排障时跨模块关联成本高。  
**建议**：在 metadata 中注入 `signal_trace_id`，全链路传递并落库。

### 2.7 诊断口径受 limit 截断影响
`strategy_diagnostics/daily_report` 基于 recent + limit，默认是样本截断视角，不是全量统计。  
**建议**：支持 DB 侧聚合（按窗口 group by）作为“权威统计”，API 默认返回聚合视图。

---

## 3) 模块化工程化建议（下一步落地顺序）

1. **接口收口**：去掉 API 对私有字段访问（P0）。
2. **初始化防抖**：加初始化锁（P0）。
3. **事件可靠性分级**：confirmed 不丢、intrabar 可丢（P1）。
4. **统一常量层**：session/regime/状态机枚举收敛到 contracts（P1）。
5. **可观测性升级**：trace_id + skipped_rows + DB 聚合诊断（P2）。

---

## 4) 关于你提出的“signal 与 risk 是否两个模块”

是的，当前设计是**分层职责**：

- `signals.filters`：信号域过滤（是否值得评估/发信号）
- `risk`：订单域风控（是否允许下单）

这是正确的架构边界，建议继续保持。

---

## 5) 本周可执行优化建议（按收益/复杂度排序）

> 进展（本次已完成）：A1 Session 枚举统一、A2 日报数据质量字段、A3 API 去私有字段耦合。  
> A1 收敛策略：开发阶段已移除旧别名兼容（`newyork` 不再自动映射），统一要求使用 `new_york`。
> 进展（B 已启动）：B1 初始化防抖（全局初始化锁），B2 confirmed 队列短阻塞回压以降低丢事件概率。
> 进展（B 持续完善）：新增 trace_id 事件查询接口（按 `signal_trace_id` 追踪链路事件）。
> 进展（B 持续完善）：新增 confirmed backpressure 观测字段（waits/failures）。
> 进展（C 已启动）：C1 新增 repository.summary 聚合视图入口，C3 新增 orchestration 包级迁移入口。

### A. 高收益低改动（建议本周完成）

1. **统一 session 枚举常量**
   - 把 `newyork` / `new_york` 统一为单一枚举。
   - 影响：过滤、诊断、日报、监控看板口径一致。
2. **日报增加数据质量字段**
   - 新增 `skipped_rows_invalid_time`、`rows_before_window_filter`。
   - 影响：避免“日报样本偏少”却无法解释。
3. **API 去私有字段耦合**
   - runtime 提供 `get_regime_stability()`；路由不再直接访问 `_regime_trackers`。
   - 影响：降低重构破坏面。

### B. 中收益中改动（建议 1~2 个迭代）

4. **初始化防抖**
   - `_ensure_initialized()` 增加全局锁 + 双重检查。
   - 影响：避免并发初始化边缘故障。
5. **confirmed 事件可靠性升级**
   - confirmed 队列满时改为 backpressure 或持久化缓冲，不直接 drop。
   - 影响：减少收盘信号丢失对交易结果的系统性影响。
6. **链路 trace_id**
   - snapshot -> decision -> persist -> execute 全链路透传 `signal_trace_id`。
   - 影响：线上排障效率显著提升。

### C. 高收益高改动（建议季度规划）

7. **诊断改为 DB 聚合优先**
   - 目前是 recent + limit 采样视角，建议增加窗口聚合 SQL 作为权威口径。
8. **策略拆包**
   - `strategies.py` 拆成 `strategies/trend.py`、`reversion.py`、`breakout.py`。
9. **orchestration 子模块化**
   - 将 runtime/voting/state machine 收敛到 `signals/orchestration/`。

### 建议的验收指标（SLO）

- `confirmed_drop_rate` < 0.1%  
- `diagnostics_time_parse_skip_rate` < 1%  
- `signal_to_order_trace_coverage` > 99%  
- `regime_api_private_field_access` = 0（必须）
