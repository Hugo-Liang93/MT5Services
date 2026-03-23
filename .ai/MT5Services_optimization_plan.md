# MT5Services 架构审查与优化计划

> 版本：基于当前主分支代码抽样审查生成  
> 审查重点：运行入口、依赖装配、市场数据、指标系统、信号运行时、持久化队列、回测模块、配置入口  
> 结论：**不建议推倒重来，建议以“先收敛边界、再拆核心大模块、最后统一回测/实盘执行路径”的方式渐进重构。**

---

## 1. 当前总体判断

你的代码库已经不是“脚本集合”，而是一个**接近完整交易平台雏形**的服务化系统，具备这些明显优点：

1. 已经形成了 `行情 → 指标 → 信号 → 风控 → 交易 → 监控` 的主链路。
2. 已经有统一入口、统一配置、持久化、监控、回测 API，不是零散模块。
3. 你已经在尝试把“实时运行”和“回测分析”收敛到同一套策略/指标体系，这是非常正确的方向。
4. 领域意识已经出现：`market / calendar / market_structure / indicators / signals / risk / trading / api` 这些边界在文档里已经有清晰目标。

**但当前最大问题不是“功能少”，而是“核心模块过重、运行时装配过集中、文档与实际边界有漂移、实时与回测共用逻辑还没真正收敛”。**

这会导致后面继续加策略、加回测、做参数优化、做 AI 调参时，维护成本和回归风险快速上升。

---

## 2. 本次审查依据（抽样）

本次不是逐文件穷举审查，而是基于主干关键文件抽样判断：

- `README.md`
- `ARCHITECTURE.md`
- `app.py`
- `src/api/__init__.py`
- `src/api/deps.py`
- `src/market/service.py`
- `src/indicators/manager.py`
- `src/signals/orchestration/runtime.py`
- `src/persistence/storage_writer.py`
- `src/backtesting/api.py`
- `src/backtesting/component_factory.py`
- `src/config/__init__.py`

---

## 3. 当前存在的核心问题

## 3.1 架构文档与实际代码边界存在漂移

### 现象
文档中已经明确希望按业务域拆分，并且指出 `src/core/` 不应继续扩张；同时强调不要再把 `compat / fallback` 当作主路径概念。  
但从实际代码看：

- README 中的目录结构仍保留了较多旧式描述。
- `src/api/__init__.py` 里已经在直接装配 `src.market`、`src.calendar`、`src.market_structure`、`src.backtesting.api`。
- `src/market/service.py` 里仍保留了 `get_ohlc()` 这类 compatibility wrapper。
- API 又同时挂载了 `/v1/...` 和“无前缀旧路由”，说明还在承担兼容负担。

### 风险
- 新成员或未来的 AI agent 很难判断“真正有效的边界”。
- 重构时容易继续在错误目录下加功能。
- 回测、策略、API、运行时会不断出现“双轨结构”。

### 建议
先做一轮**架构收敛**，让文档、目录、装配方式三者统一，不再出现“文档说 A，代码正在做 B”的情况。

---

## 3.2 `src/api/deps.py` 过于集中，已经成为运行时总控大文件

### 现象
`src/api/deps.py` 目前承担了：

- 全局容器定义
- 单例初始化控制
- 各领域组件构建
- startup 状态记录
- runtime task 状态写入
- 依赖注入函数
- lifespan 绑定

这已经不只是依赖模块，而是**应用运行时总装配中心**。

### 风险
- 全局单例 + 隐式初始化，对测试、回测隔离、多 worker 部署都不友好。
- 任一领域变更都可能波及这个文件。
- 一旦要支持多账户、多实例、多策略沙箱、回测 worker，很容易出现状态污染。
- 未来你如果做 AI 自动调参或批量回测，很难复用这一套初始化逻辑而不互相干扰。

### 建议
把它拆成三层：

1. `AppContainer`：只负责组件对象持有，不做逻辑。
2. `AppBuilder`：只负责 build，不启动线程。
3. `AppRuntime`：只负责 start / stop / health / status。

这样 API、CLI、回测、测试都能共用“构建逻辑”，但不共用“全局单例状态”。

---

## 3.3 `MarketDataService` 责任过重，已经混合了缓存、查询、事件、回写、线程管理

### 现象
`src/market/service.py` 当前至少承担：

- MT5 行情读取
- Tick/Quote/OHLC 内存缓存
- 缓存与 DB fallback 读取
- OHLC close 事件通知
- intrabar 监听器分发
- indicator 回写
- listener 线程池管理

### 风险
这个类已经不只是“Market Service”，而是：

- 数据缓存层
- 查询层
- 事件总线
- 部分读模型拼接层

全部混在一起。

后面你要做：
- 更细的 cache policy
- 多 symbol 扩容
- 回测读取适配
- replay 模式
- 本地 CSV / parquet 数据源
时，都会被这个类卡住。

### 建议
拆成下面 3 个组件：

- `MarketCacheStore`：只管 Tick/Quote/OHLC 的内存状态
- `MarketQueryService`：只负责“从 cache / DB / live source 取数据”
- `MarketEventBus`：只负责 close / intrabar 事件订阅和发布

`MarketDataService` 可以保留成 façade，但不再直接承载所有细节。

---

## 3.4 `UnifiedIndicatorManager` 已经成为指标域的“超级对象”

### 现象
`src/indicators/manager.py` 同时管理：

- 指标配置加载
- pipeline 注册
- dependency manager
- event store
- closed-bar queue / writer thread / intrabar thread / reload thread
- reconcile
- snapshot 发布
- 结果缓存
- preview snapshot
- indicator write-back
- 持久化 enqueue

### 风险
这会带来两个问题：

1. **指标计算逻辑和运行时编排耦合过深**  
   你以后想单独复用“指标计算核心”去做回测、参数搜索、离线训练，会很重。

2. **线程/事件/缓存/配置/持久化全部集中到一个类**  
   一旦改动 intrabar、增量指标、重载机制，很容易影响整条链路。

### 建议
把指标域拆成：

- `IndicatorRegistry`：指标定义与配置
- `IndicatorComputeService`：纯计算
- `IndicatorEventProcessor`：事件消费 + reconcile
- `IndicatorSnapshotPublisher`：快照发布
- `IndicatorResultRepository`：结果缓存/持久化适配

重构目标不是删功能，而是让“离线计算”和“实时事件驱动”共用同一个 `compute core`。

---

## 3.5 `SignalRuntime` 过重，信号域目前最需要做边界拆分

### 现象
`src/signals/orchestration/runtime.py` 同时处理：

- snapshot 入队与消费
- confirmed / intrabar 双队列
- regime 检测
- session / spread / filter
- strategy scope / timeframe / affinity gate
- market structure 注入
- HTF 指标注入
- intrabar decay
- voting engine
- runtime state machine
- emit / persist / restore state
- loop backoff 与降级监控

### 风险
这是典型的**交易运行时总线 + 状态机 + 评估器 + 编排器**混在一起。

后面如果你想做：
- 策略集群对比
- A/B 策略路由
- 回测 / 实盘同逻辑校验
- AI 参数搜索
- 信号质量归因
都会越来越难。

### 建议
至少拆成四层：

1. `SnapshotIntake`：队列与去重
2. `SignalEvaluator`：Regime / filter / HTF / market_structure / evaluate
3. `SignalStateMachine`：preview / armed / confirmed 转移
4. `SignalEmitter`：persist / publish / execute hook

然后让 `SignalRuntime` 只剩下 orchestration。

---

## 3.6 当前队列体系仍有“关键事件可能丢失”的风险

### 现象
从现有实现看：

- `MarketDataService` 的 OHLC close event queue 可能满后丢弃
- `SignalRuntime` confirmed 队列虽然优先，但回压失败后仍可能丢弃 confirmed 事件
- `StorageWriter` 根据不同策略会 block / drop_oldest / drop_newest

### 风险
对于交易系统，队列事件不能一概同等对待。

应区分：

- **可丢弃**：intrabar preview、调试快照、部分统计
- **不可丢弃**：confirmed close、交易信号、订单执行记录、风控阻断记录

现在的代码已经有这方面意识，但策略还不够彻底。

### 建议
建立“事件等级”：

- L1：必须持久化，不能丢（confirmed bar、trade signal、risk decision、order execution）
- L2：允许回放恢复（indicator snapshot）
- L3：最佳努力即可（intrabar preview、部分监控）

然后统一到一个明确策略：
- L1 走 durable event store
- L2 可以 queue + replay
- L3 允许 drop

---

## 3.7 回测模块已经存在，但目前更像“研究型实现”，还不是生产级回测平台

### 现象
`src/backtesting/api.py` 当前使用：

- FastAPI `BackgroundTasks`
- 进程内 `_results_store`
- 单 semaphore 控制并发
- 完成后 best-effort 持久化
- API 直接发起较重任务

`src/backtesting/component_factory.py` 里还通过 fake config 调用运行时内部逻辑去覆盖参数。

### 风险
这说明当前回测已经“能用”，但有几个明显局限：

1. 回测任务生命周期不独立于 API 进程。
2. 结果状态部分靠内存，进程重启后体验不稳定。
3. 参数覆盖依赖运行时内部实现，耦合较深。
4. 回测和实时运行还不是完全共享同一套“执行内核”。

### 建议
把回测重构为：

- `BacktestJobService`
- `BacktestRunner`
- `ParameterSearchRunner`
- `BacktestResultRepository`

让 API 只负责“提交任务 / 查任务 / 查结果”，不直接承担运行器职责。

---

## 3.8 配置入口已经统一，但配置面仍然偏宽，容易继续膨胀

### 现象
`src/config/__init__.py` 对外 re-export 了大量 getter / settings / loader。  
这说明你已经在努力统一配置入口，但同时也意味着：

- 运行时配置
- 数据库配置
- MT5 配置
- 指标配置
- runtime market/ingest settings
- economic/signal/risk

全部都从公共入口泄露出来。

### 风险
- 模块很容易随手 import 任意配置，而不是依赖自己真正需要的那一小块。
- 以后要做测试替换、环境切换、配置校验分层时会比较痛苦。
- 回测配置和实盘配置边界容易模糊。

### 建议
分成三类：

- `RuntimeConfig`：服务启动、线程、队列、监控
- `TradingDomainConfig`：signal / risk / strategy / session
- `InfraConfig`：db / mt5 / storage / auth

并明确“模块只能拿自己的 config slice”。

---

## 3.9 API 版本化策略还没完全收敛

### 现象
`src/api/__init__.py` 目前同时挂：

- `/v1/...`
- 无前缀旧路由

### 风险
短期兼容没问题，但长期会带来：

- 文档重复
- SDK 与调用方认知混乱
- 鉴权和中间件策略维护成本增加
- 未来 `/v2` 演进时负担更大

### 建议
明确一个阶段性策略：

- 现在开始：所有新接口只走 `/v1`
- 旧路由进入 deprecated 名单
- 提供迁移窗口
- 后续一个版本移除无前缀路由

---

## 4. 优化优先级判断

## P0（立刻做）
这些不做，后面继续加功能会越来越乱。

1. 文档与实际目录/装配方式收敛
2. 依赖装配层拆分（至少把 `deps.py` 降重）
3. 回测与实时运行共用核心逻辑的边界定义
4. 关键事件“不可丢失”分级梳理

## P1（接下来优先）
这些决定你后面能不能稳定扩策略、做参数优化。

1. 拆 `MarketDataService`
2. 拆 `UnifiedIndicatorManager`
3. 拆 `SignalRuntime`

## P2（第二阶段）
这些决定系统能不能规模化演进。

1. 引入独立 backtest job runner
2. 配置切片化
3. 统一运行时健康与队列告警模型
4. 增加 contract test / integration test

---

## 5. 推荐重构路线图

## Phase 0：架构收敛（1 周）

### 目标
先统一“你想怎么做”和“代码正在怎么做”。

### 输出物
- `docs/architecture/current-runtime-map.md`
- `docs/architecture/module-boundaries.md`
- `docs/architecture/refactor-target.md`

### 任务
- 列出真实生效目录，而不是理想目录
- 给每个模块标注 owner 和职责
- 标出临时兼容层、历史残留层、计划删除层
- 写清楚“实时 / 回测 / API / 测试”各自如何构建组件

### 验收标准
- 新人只看 3 份文档就知道该把新功能放在哪
- 不再出现 README、ARCHITECTURE、实际 import 三套说法

---

## Phase 1：应用启动与依赖装配重构（1~2 周）

### 目标
让启动逻辑从“全局隐式单例”变成“显式构建、显式启动、显式关闭”。

### 任务
- 新建 `src/app_runtime/`
- 拆分：
  - `container.py`
  - `builder.py`
  - `runtime.py`
  - `health.py`
- `src/api/deps.py` 退化成 API adapter
- API、CLI、回测均调用同一套 builder
- 避免业务层直接依赖全局对象

### 验收标准
- 可以单独构建一个 runtime 实例用于测试
- 可以构建“只跑 backtest、不起 API”的进程
- 可以构建“只起 signal runtime”的轻量测试实例

---

## Phase 2：市场数据层拆分（1 周）

### 目标
把市场域从“神类”拆成可替换部件。

### 目标结构
```text
src/market/
├─ cache_store.py
├─ query_service.py
├─ event_bus.py
├─ live_source.py
└─ facade.py
```

### 任务
- 把 cache 挪到 `MarketCacheStore`
- 把 DB/live fallback 读逻辑挪到 `MarketQueryService`
- 把 close/intrabar 事件发布挪到 `MarketEventBus`
- 保留 `MarketDataService` 作为 façade 过渡一段时间

### 验收标准
- 回测数据源可以替换 live source
- 指标模块只依赖 query 接口，不依赖整块市场服务
- 事件通知不再和缓存读写绑死

---

## Phase 3：指标系统拆分（1~2 周）

### 目标
实现“计算核心”和“实时调度”分离。

### 目标结构
```text
src/indicators/
├─ registry/
├─ compute/
├─ runtime/
├─ publishing/
└─ repositories/
```

### 任务
- `IndicatorRegistry` 管配置与注册
- `IndicatorComputeService` 只负责 bars → indicators
- `IndicatorEventProcessor` 负责消费 closed/intrabar event
- `IndicatorSnapshotPublisher` 负责对外广播
- `IndicatorResultRepository` 负责缓存与落库适配

### 验收标准
- 回测可直接调用 compute service，无需起完整 manager
- intrabar / confirmed / reconcile 路径都走同一个计算内核
- 新增指标不会再碰运行时线程代码

---

## Phase 4：信号运行时拆分（2 周）

### 目标
让信号系统真正成为可测试、可回放、可复用的“决策引擎”。

### 目标结构
```text
src/signals/
├─ intake/
├─ evaluator/
├─ state_machine/
├─ orchestration/
├─ emitters/
└─ diagnostics/
```

### 任务
- snapshot intake 负责队列、去重、反饥饿
- evaluator 负责 filter / regime / HTF / market_structure / strategy evaluate
- state machine 负责 preview/armed/confirmed/cancelled
- emitter 负责 persist / publish / trade hook
- runtime orchestration 只负责把这些步骤串起来

### 验收标准
- 单独测试 state machine，不依赖完整 runtime
- 单独测试 evaluator，不依赖线程和队列
- 回测可以复用 evaluator + state machine，而不是重走一套影子逻辑

---

## Phase 5：回测平台化（1~2 周）

### 目标
把当前“API 内嵌回测”升级成真正可扩展的回测与优化服务。

### 任务
- 新建 `BacktestJobService`
- API 改为：
  - submit job
  - query status
  - query result
  - cancel job
- 任务结果全部入库
- 移除对 in-memory `_results_store` 的核心依赖
- 参数覆盖逻辑从 API/factory 内部函数中解耦
- 统一 live/backtest 的 strategy execution adapter

### 验收标准
- API 重启后仍能看到历史任务状态
- 参数优化不再依赖进程内内存状态
- 回测与实时评估的差异可以被显式比较

---

## Phase 6：可靠性与质量门禁（持续）

### 目标
降低“改一个策略，线上链路全变”的风险。

### 任务
- 增加测试层级：
  - unit test：indicator / strategy / risk rule / state machine
  - contract test：API schema / config schema
  - integration test：market → indicator → signal → trade mock
  - replay test：用历史 bars 回放，检查 live/backtest 一致性
- 增加质量门禁：
  - lint
  - type check
  - smoke test
  - minimal integration test
- 队列事件按等级落监控指标
- 对 confirmed signal / order execute / risk block 做审计表

### 验收标准
- 每次改策略前后可以做最小回归验证
- live 路径和 backtest 路径差异可观测
- “事件丢失”能监控到，不靠看日志猜

---

## 6. 建议的目标目录形态（中期）

```text
src/
├─ api/
├─ app_runtime/
├─ market/
├─ calendar/
├─ market_structure/
├─ indicators/
├─ signals/
├─ risk/
├─ trading/
├─ backtesting/
├─ monitoring/
└─ infra/
   ├─ config/
   ├─ persistence/
   ├─ mt5/
   └─ auth/
```

### 说明
- `clients/`、`persistence/`、部分 `config/` 最终建议逐步向 `infra/` 收敛
- `backtesting/` 保持独立，但必须复用 `indicators` 与 `signals` 的核心计算能力
- `app_runtime/` 专门承接原来 `deps.py` 的装配职责

---

## 7. 你当前最值得先改的 5 件事

1. **先拆 `src/api/deps.py`**  
   这是整个系统继续演进的总瓶颈。

2. **给 `SignalRuntime` 做边界切分设计图**  
   因为它是后面回测统一、策略扩展、AI 调参的核心。

3. **把回测改成 job 模式，而不是 API 进程内 BackgroundTasks**  
   这样未来才能做参数搜索、批量优化。

4. **建立关键事件不可丢失分级**  
   confirmed signal / risk decision / execution record 必须 durable。

5. **补一份“实时与回测共用内核”的设计文档**  
   这是你以后做自动调参、策略比较、训练评估的基础。

---

## 8. 建议的实施顺序（最务实版本）

### 第 1 周
- 补文档
- 拆 `deps.py`
- 明确 runtime/container/builder 边界

### 第 2 周
- 拆 market service
- 建立统一 query / event 抽象

### 第 3~4 周
- 拆 indicator manager
- 拆 signal runtime
- 写最小 replay test

### 第 5 周
- 回测 job 化
- 参数优化接口重构

### 第 6 周以后
- AI 调参
- walk-forward
- live/backtest 一致性审计
- 策略质量归因面板

---

## 9. 最终结论

你的仓库已经具备了继续做大的基础，**最大问题不是“缺功能”，而是“核心对象太重、运行时边界不够稳定”**。

所以最优策略不是重写，而是：

1. **先收敛边界**
2. **再拆大模块**
3. **再统一 live/backtest 执行内核**
4. **最后再接 AI 自动调参与优化**

只要这一步走对，后面你想做：

- MT5 实盘 + Python 服务分层
- 历史回测
- 参数优化
- Walk-forward
- AI 自动调参
- 信号质量归因
- 多策略路由

都会顺很多。

---

## 10. 我建议你下一步立即产出的文档

建议在仓库里新增这 4 个 md：

1. `docs/architecture/current-runtime-map.md`
2. `docs/architecture/module-boundaries.md`
3. `docs/refactor/deps-split-plan.md`
4. `docs/refactor/live-backtest-unification-plan.md`

---

## 11. 附：可直接执行的首轮重构任务清单

```text
[ ] 新建 docs/architecture/current-runtime-map.md
[ ] 新建 docs/architecture/module-boundaries.md
[ ] 新建 src/app_runtime/container.py
[ ] 新建 src/app_runtime/builder.py
[ ] 新建 src/app_runtime/runtime.py
[ ] 将 src/api/deps.py 中的 build/start/stop 分离
[ ] 为 MarketDataService 设计 cache/query/event 三分结构
[ ] 为 UnifiedIndicatorManager 设计 compute/runtime 分离结构
[ ] 为 SignalRuntime 设计 intake/evaluator/state_machine/emitter 分离结构
[ ] 将 backtesting/api.py 改为 submit/query 风格
[ ] 去除 backtesting 对 fake config + api 内部函数的隐式依赖
[ ] 定义关键事件 durable 等级
[ ] 增加 live vs backtest replay consistency test
```

---

## 12. 一句话策略

**先把“系统怎么启动、模块怎么装配、实时和回测怎么共用内核”这三件事定住，再去继续扩策略和做 AI 优化。**