# 模块化审计（signals / indicators / trading）

> 范围：`src/signals`, `src/indicators`, `src/trading`  
> 更新日期：2026-04-11  
> 目标：发现“模块边界不清晰”的高风险点，不做重构式大改，给出可执行整改序列。

## 一、模块边界现状

## 1) signals

### 现状
- 目录已按职责拆分为 `orchestration`/`strategies`/`execution`/`evaluation`/`tracking`，方向正确。
- 关键门面文件：
  - `service.py`（697 行）负责策略加载与统一入口。
  - `orchestration/runtime.py` 已拆分为职责组件：`RuntimePolicyCoordinator`、`RuntimeLifecycleManager`、`QueueRunner`、`SignalLifecyclePolicy`、`RuntimeStatusBuilder`，降低 `runtime.py` 的单点职责压力。
  - `orchestration/runtime_warmup.py` 与 `runtime_status.py` 已明显分离初始化与可观测逻辑。
- 已有能力收口：`strategy_capability_catalog/contract` 已被 `runtime` 和 `backtesting` 共享。

### signals 边界巡检清单（本轮）
- 输入：
  - `SignalModule`：策略能力、`evaluate()` 与 `persist_decision()`；  
  - `snapshot_source`：快照监听器事件；
  - `filter_chain`：过滤、会话、指标风险过滤。  
- 编排：
  - `runtime.py` 负责主流程拼接；  
  - `runtime_components.py` 负责生命周期、队列、状态机和快照构建；  
  - `runtime_processing.py` 负责事件处理/判定；  
  - `runtime_evaluator.py` 负责策略评估与决策发布；  
- 输出：
  - `SignalRuntime` 对外只有 listener/状态/能力端口；  
  - 下游消费侧通过 `add_signal_listener` 与 `status()` 获取边界结果；  
- 状态写入拥有者：
  - 生命周期状态、线程状态：`RuntimeLifecycleManager`；  
  - 队列与监听器：`QueueRunner`；  
  - 策略投票/目标索引：`RuntimePolicyCoordinator`；  
  - 运行统计：`RuntimeStatusBuilder`。

### 风险点（P0/P1）
- `service.py` 仍混合了策略注册、评估编排、诊断读写，职责仍偏宽。
- 部分策略执行状态的观测路径依然跨文件拼接，需避免 API/展示层继续读取私有字段（当前虽减少，但仍要持续清理）。

### 最小整改建议
1. 在 `service.py` 增加“只读能力视图”公开方法与策略-能力契约文档，减少外部模块对策略注册细节依赖。

## 2) indicators

### 现状
- 目录已完成“管理器 + 执行时/查询时分离”：
  - `manager.py` 仅 222 行，主入口；
  - `lifecycle.py` 负责线程生命周期；
  - `pipeline_runner.py`、`query_runtime.py`、`query_read.py` 分离运行时/查询职责；
  - `snapshot_publisher.py`/`event_io.py`/`query_read.py` 承接发布与观察链路。
- `engine/` 与 `core/` 分层清晰，核心指标函数和执行引擎已解耦。

### 风险点（P0/P1）
- `query_*` 与 `registry_*` 子模块仍大量直接访问 `manager._xxx` 私有字段（例如 `manager._results`、`_results_lock`、`_last_preview_snapshot` 等）。这使“状态拥有方”边界依赖变弱、测试时难以替换/mock。
- `event_loop`/`intrabar` 路径仍存在容量/延迟退化未统一观测；目前可见性不足时，压测时出现 silent drop 风险。
- `pipeline` 与 `snapshot` 阶段的回放/降级策略尚缺少统一指标口径（丢弃率/队列老化）。

### 最小整改建议
1. 增加 `UnifiedIndicatorManager` 的最小公开端口：结果、队列、快照订阅、指标元数据变更应通过端口调用，不再直接读私有集合。
2. 将 intrabar 与事件队列的 drop/age 指标补齐到现有监控输出（`indicators.monitoring` 侧）。
3. 在 `registry_runtime.py`、`query_runtime.py` 的初始化路径补充单测，定义“无私有字段读取”的替代路径。

## 3) trading

### 现状
- `application`、`execution`、`positions`、`pending`、`tracking` 等子包已形成层次。
- `execution/` 已有 `pre_trade_pipeline` / `decision_engine` / `result_recorder` 等拆分。
- `trading_service.py` 仍承担 MT5 交易适配边界转换。

### 风险点（P0/P1）
- `execution/executor.py`（651 行）与 `positions/manager.py`（785 行）仍属于高集中度组件。
- 生命周期约束仍分散：多处 `join` 后清理线程引用的策略不一致，需要统一策略契约（`is_running` 与引用保留一致）。
- `pending/manager.py` 与 `positions/manager.py` 之间仍有状态时序耦合，需要更清晰状态写入点。
- 装配层不能代替领域改造：`factory` 只能作为端口装配入口，字段映射/默认策略需回写到领域层契约或服务端口实现。

### 最小整改建议
1. 已抽取 `src/trading/runtime_lifecycle.py`，用 `OwnedThreadLifecycle` 统一 `TradeExecutor` 与 `PositionManager` 的线程启停行为（启动、wait、stop、is_running）。
2. 先对 `executor.py` 与 `positions/manager.py` 的生命周期、状态快照、事件发布三个方向做职责“收缩线”：把状态查询类接口固定为只读快照返回值。
3. 在 `execution` 与 `positions` 之间补充“交易状态转移契约”（状态枚举 + 事件名），并已同步到 `src/trading/trade_events.py`（2026-04-11）。

## 二、任务清单（小规模）

- [ ] 任务 A：补齐 `indicators` 私有字段读访问替代端口（P1，1 周）
  - 影响文件：`src/indicators/query_read.py`、`query_runtime.py`、`registry_runtime.py`
- [x] 任务 B：将 `signals/runtime.py` 的事件持久化副作用收口到现有子组件（P1，1 周）
- [x] 任务 C：将 `trading/executor.py` 与 `positions/manager.py` 的线程停止契约统一（P1，3 天）
- [x] 任务 E：新增 `src/trading/trade_events.py`，统一 `execution/positions` 的状态原因语义，剥离散落字符串（2026-04-11）
- [ ] 任务 D：补充 3 个跨模块 smoke 回归（P1）
  - indicator 管理器端口可调用性（无 private 访问）
  - runtime status 与入口一致（只走 status/contract）
  - 执行链路命令可观测日志不丢字段

## 三、结论

三大领域已经有明显的分层雏形，但当前风险不在“新增模块缺失”，而在：
1. 高复杂度类中的职责再增长；
2. 私有状态可见性仍可见到多处跨层引用。

建议下一步先按 任务 A/B/C 处理，不采用再写“兼容回退”或“临时分支”。
