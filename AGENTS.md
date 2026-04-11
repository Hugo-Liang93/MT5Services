# AGENTS.md — MT5Services Agent Working Rules

本文件面向在本仓库中执行任务的 AI Agent，内容依据 `CLAUDE.md` 提炼为可执行规则。

## 1) 交流与输出

- 所有对用户的回复、解释与建议默认使用**简体中文**。
- 改动说明应聚焦“做了什么 / 为什么 / 如何验证”。

## 2) 技术栈与运行入口

- 核心技术栈：FastAPI + uvicorn，数据库为 TimescaleDB（PostgreSQL）。
- 常用启动方式：
  - `python -m src.entrypoint.web`
  - `uvicorn src.api:app`

## 3) 目录认知（高频）

- `src/api/`：HTTP 路由、Schema、DI 适配。
- `src/config/`：配置加载与模型。
- `src/indicators/`：指标管理、计算引擎、缓存与监控。
- `src/signals/`：信号策略、编排、过滤、评估与追踪。
- `src/trading/`：交易执行、准入、仓位与结果追踪。
- `src/persistence/`：数据库写入、队列持久化、仓储层。
- `src/backtesting/`：回测、优化、前推与参数推荐。
- `docs/codebase-review.md`：当前代码库审查风险台账；做架构、策略、性能整改前先阅读并更新。
- `tests/`：测试套件（结构与 `src/` 对齐）。

## 4) 配置系统硬性规则

- **`config/app.ini` 是交易品种、时间框架、全局采集间隔的唯一事实源（SSOT）**。
- 禁止在其他 `.ini` 文件重复定义上述核心参数。
- 配置优先级（高→低）：
  1. `config/*.local.ini`
  2. `config/*.ini`
  3. 代码默认值（`src/config/centralized.py`）
- `.env` 已废弃；新增配置应优先进入 `.ini` 配置体系。
- `config/*.local.ini` 虽不入库但会优先生效；审查运行问题时必须把 local 覆盖纳入分析，尤其是 `signal.local.ini` 中的策略、投票组、regime affinity 覆盖。
- local 覆盖不应保留已删除策略名；若发现 `strategy_sessions`、`strategy_timeframes`、`voting_groups`、`regime_affinity.*` 指向未注册策略，应优先视为配置漂移风险，而不是在代码中做兼容分支。

## 5) 信号参数配置约定

- 优先通过 `config/signal.ini` 调参数，避免直接改策略源码常量。
- 策略参数查找优先级：
  - `[strategy_params.<TF>]`
  - `[strategy_params]`
  - 策略代码默认值
- 当前默认策略体系是结构化策略目录 `src/signals/strategies/structured/` 中注册的 8 个实例；不得按旧 README/旧 local 配置假设仍存在 legacy 策略或 composites。
- 投票组配置必须只引用已注册策略；一旦 `voting_groups` 非空，全局 consensus 会自动关闭，配置错配会导致“没有结构化共识信号”的隐性退化。

## 6) 代码修改原则

- 先定位现有模块职责，再做最小必要改动，避免跨模块“顺手重构”。
- 保持与现有命名、分层、依赖方向一致。
- 新逻辑优先放在对应领域目录，避免“临时工具”散落。
- 默认禁止以“兼容补丁”“临时兜底”“额外分支特判”作为功能开发或优化的主要实现方式；应优先从领域边界、状态模型、模块职责出发，做可持续的工程化设计。
- 如需调整现有能力，优先重构到清晰模块或正式抽象中，再迁移调用方；不要为了短期兼容而长期保留双轨语义、别名字段或历史行为映射，除非用户明确要求兼容过渡。
- 后续修改默认要优先考虑整体工程化与模块化：功能模块之间职责单一、依赖方向清晰、跨模块交互通过正式端口/服务完成，避免隐式能力探测和跨层直连。
- 优化时优先按职责边界收敛，而不是堆新配置、新开关或新抽象；若抽象不能明显减少耦合、统一语义或提升可审查性，则视为过度设计风险。
- 禁止为了“先跑起来”把命令、查询、状态持久化、监控投影、运行编排混在同一入口中；应优先拆成独立应用服务、领域服务、读模型或状态服务。
- 禁止 API、readmodel、studio 或 app_runtime 为了取状态而探测私有属性/方法；若确实需要状态，先补正式公开端口，再迁移调用方。
- 后台线程组件的 `stop()` 不能在 `join(timeout)` 后无条件清空线程引用；只有确认线程退出后才清引用，仍存活时必须保留引用并让 `is_running()` 反映真实状态。
- 涉及配置项新增时，需同时更新：
  - 配置加载模型（`src/config/`）
  - 默认配置文件（`config/*.ini`）
  - 必要文档/注释

### 6.1) 模块化工程化强制规范（新增）

- 所有新开发/重构工作默认执行“单一职责边界+单向依赖”：
  - 一个模块只对一个问题域负责。
  - 跨模块交互仅通过公开端口（方法/接口/数据结构），避免直接读写其他模块内部细节。
- 任何存在重复功能的实现必须先合并到共享服务模块，不允许并行保留同功能冗余实现（除非有明确性能或历史兼容性定量证明）。
- 重构优先顺序固定为：职责识别 → 接口收口 → 语义统一 → 移除旧实现，不以兼容分支掩盖新设计。
- 不允许为“历史行为”长期保留以下模式：
  - 兼容参数别名
  - 分支兜底
  - 双轨运行路径
  - 以“兼容旧版本”名义新增临时路径
- 对高风险模块（`indicators`、`signals/orchestration`、`trading`）的改动，PR 级别必须至少满足：
  - 模块边界图说明（输入/输出/状态拥有方）
  - 变更前后职责差异说明
  - 冗余实现清理清单（新增、保留、移除）

## 7) 提交前检查建议

- 至少执行与改动直接相关的测试或静态检查。
- 若受环境限制无法运行，需在结果中明确说明限制与影响面。

## 8) 风险变更提醒

以下变更属于高风险，需额外谨慎并补充验证：

- 交易执行链路（`src/trading/`、`src/api/trade_dispatcher.py`）
- 风控规则（`src/risk/`、`config/risk.ini`）
- 指标与信号编排主链路（`src/indicators/manager.py`、`src/signals/orchestration/`）
- 数据持久化与队列策略（`src/persistence/`、`config/storage.ini`）

## 9) 文档同步

- 若改动影响架构、流程或关键配置，需同步更新相关文档（如 `docs/architecture.md`、`docs/design/adr.md`、`docs/codebase-review.md`）。
- 若改动触及“信号生成 → 过滤 → 风控 → 执行交易”主链路，需同步考虑该链路的可视化与可审查性，确保后续能够定位：
  - 当前信号来自哪个策略、哪个阶段被过滤或阻断
  - 当前交易在执行链路的哪个节点失败
  - 当前持仓/挂单/风险控制状态与上游信号之间的关联关系

## 10) 可观测性与数据流

- 后续架构优化应优先支持关键数据流节点可视化，尤其是：
  - 市场数据接收
  - 指标计算
  - 信号生成
  - 信号过滤 / 风控阻断
  - 交易执行
  - 挂单与持仓管理
- 对交易主链路，优先建设“可追踪、可投影、可审查”的结构化状态与事件，而不是仅依赖日志文本排查。
- 设计新模块时，应优先回答：
  - 它在整条链路中的职责是什么
  - 它的输入/输出事实源是什么
  - 它失败后应如何被定位和观测
- 若某项改动会让链路更难追踪、更难可视化或更难定位问题，应视为设计退化，默认不采用。

## 11) Working Principles

You must reason from first principles. Start from the real objective, constraints, and root problem rather than conventions, templates, or the user's proposed path.

Rules:
1. If the user's goal, motivation, or success criteria are unclear, pause and clarify before proposing implementation.
2. If the user's requested path is not the shortest or most effective path, say so directly and propose a better one.
3. Always prefer root-cause analysis over surface-level fixes. Every non-trivial recommendation must answer: why this decision?
4. Keep outputs decision-focused. Include only information that changes action, architecture, risk, or tradeoffs.
5. Do not optimize for agreement. Optimize for correctness, clarity, and leverage.
6. If assumptions are necessary, state them explicitly and minimize them.

## 12) 后续协作执行纪律（高优先级）

- 每次新任务开始前，默认执行“四步门禁”，未通过不得进入实现：
  1. 范围确认：明确本次改动是否触及高风险链路（`indicators`、`signals/orchestration`、`trading`、`risk`、运行时主链路）。
  2. 边界自检：校验改动模块是否有清晰输入/输出/状态归属定义，以及是否出现跨域私有字段访问（`._xxx`）意图。
  3. 异常边界归类：明确每个新增异常分支是“必须失败”还是“可降级”；同一职责内不允许无差别 `except Exception` 分散扩散。
  4. 合同验证：检查新增/变更端口是否已有正式契约，避免用兼容分支替代接口重构。
- 若发现与第 6 节/6.1 节冲突，默认执行优先级为：
  1) 收口边界，
  2) 清理重复实现，
  3) 才能回退到兼容过渡（且必须写入时限与移除计划）。
- 约束执行后置项：
  - 任何代码提交默认要求说明“本次改动如何减少边界泄漏/减少兼容分支”。
  - 必须更新 `docs/codebase-review.md` 的本次变更条目（包括新增职责变化、移除兼容路径、未决项）。
  - 任何保留兼容分支都需给出“移除截止条件与时间窗口”，不得默认长期保留。
- 例外规则：只有用户明确要求“兼容过渡”时，才能引入临时兼容路径；临时路径必须写在对应模块备注/文档中，限定范围和清退时间。

---

如与更深层目录中的 `AGENTS.md` 冲突，以更深层文件为准；如与系统/开发者/用户指令冲突，以后者为准。
