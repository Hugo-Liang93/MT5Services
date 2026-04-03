# 系统数据流

本文档描述当前版本的数据生成、消费、持久化与前端供给链路。

## 1. 总体链路

```text
MT5
-> BackgroundIngestor
-> MarketDataService
-> UnifiedIndicatorManager
-> SignalRuntime
-> TradeExecutor / TradingModule
-> PositionManager
```

并行的持久化与观测链路：

```text
BackgroundIngestor -> StorageWriter -> TimescaleDB
Indicator events -> runtime_data_dir/events.db
Monitoring metrics -> runtime_data_dir/health_monitor.db
PipelineEventBus -> PipelineTraceRecorder -> pipeline_trace_events
```

## 2. 数据生成链路

### 2.1 行情采集

`BackgroundIngestor` 周期性从 MT5 拉取：

- quote
- ticks
- closed bars
- intrabar bar

写入目标：

- `MarketDataService`
- `StorageWriter`

其中：

- `MarketDataService` 用于运行时实时消费
- `StorageWriter` 用于异步落库

### 2.2 指标计算

`UnifiedIndicatorManager` 消费两类输入：

- 收盘事件
- 盘中更新

输出：

- confirmed snapshot
- intrabar snapshot
- 性能统计与事件统计

### 2.3 信号评估

`SignalRuntime` 消费指标快照并执行：

- SignalFilterChain
- Regime 检查
- 策略评估
- Confidence 校正
- Voting
- ExecutionGate

输出：

- SignalEvent
- 信号跟踪记录
- 自动交易触发
- Pipeline trace 过滤/评估事件

### 2.4 交易执行

`TradeExecutor` 负责：

- 将 confirmed signal 转为交易请求
- 调用 `TradingModule.dispatch_operation("trade")`
- 与 `PendingEntryManager`、`PositionManager` 协作

## 3. 持久化链路

### 3.1 TimescaleDB

`StorageWriter` 负责多通道异步落库，典型数据包括：

- quotes
- ticks
- ohlc
- ohlc_indicators
- signal_records
- signal_outcomes
- trade_outcomes
- trade_command_audits

除此之外，交易主链路可视化相关的结构化事实表包括：

- `pipeline_trace_events`
- `pending_order_states`
- `position_runtime_states`

### 3.2 本地运行时状态

- `runtime_data_dir/events.db`
  - 指标收盘事件 durable store
- `runtime_data_dir/health_monitor.db`
  - 健康监控指标与告警
- `runtime_data_dir/calibrator_cache.json`
  - calibrator 缓存

## 4. 前端与运维供给链路

### 4.1 Admin / Monitoring

当前统一改为：

```text
运行时组件
-> RuntimeReadModel
-> /v1/admin/*
-> /v1/monitoring/*
```

`RuntimeReadModel` 当前统一输出：

- dashboard_overview
- health_report.runtime
- trading_summary
- storage_summary
- indicator_summary
- signal_runtime_summary
- trade_executor_summary
- pending_entries_summary
- tracked_positions_payload

交易主链路 Trace 则独立为：

```text
pipeline_trace_events
+ signal_events / auto_executions / trade_command_audits
+ pending_order_states / position_runtime_states / trade_outcomes
-> TradingFlowTraceReadModel
-> /v1/trade/trace/{signal_id}
```

这样可以避免：

- `admin` 自己拼一套
- `monitoring` 再拼一套
- 前端拿到不同字段口径

### 4.2 Studio

Studio 供给链路独立为：

```text
运行时组件
-> src/studio/runtime.py
-> StudioService
-> /v1/studio/*
```

Studio 装配层负责：

- agent 注册
- summary provider 注册
- signal / trade 事件桥接

## 5. 监控链路

监控注册由 `AppRuntime` 完成，不由 API 层兜底：

```text
AppRuntime._register_monitoring()
-> data_ingestion
-> indicator_calculation
-> market_data
-> economic_calendar
-> signals
-> trading
-> pending_entry
```

`MonitoringManager` 周期性采集：

- queue_stats
- indicator_freshness
- performance_stats
- economic_calendar
- status
- monitoring_summary
- pending_entry

## 6. 路由暴露规则

当前业务接口只保留版本化暴露：

- `/v1/market/...`
- `/v1/trade/...`
- `/v1/signals/...`
- `/v1/monitoring/...`
- `/v1/admin/...`
- `/v1/studio/...`

根路径只保留：

- `/health`

## 7. 设计约束

- 运行时状态查询优先进入 `src/readmodels/`
- 展示层适配优先进入 `src/studio/`
- API 层不再重复聚合业务运行时摘要
- 监控组件注册不再在接口层手工补数据
