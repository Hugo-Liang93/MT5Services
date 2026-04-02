# AGENTS.md — MT5Services Agent Working Rules

本文件面向在本仓库中执行任务的 AI Agent，内容依据 `CLAUDE.md` 提炼为可执行规则。

## 1) 交流与输出

- 所有对用户的回复、解释与建议默认使用**简体中文**。
- 改动说明应聚焦“做了什么 / 为什么 / 如何验证”。

## 2) 技术栈与运行入口

- 核心技术栈：FastAPI + uvicorn，数据库为 TimescaleDB（PostgreSQL）。
- 常用启动方式：
  - `python app.py`
  - `uvicorn src.api:app`

## 3) 目录认知（高频）

- `src/api/`：HTTP 路由、Schema、DI 适配。
- `src/config/`：配置加载与模型。
- `src/indicators/`：指标管理、计算引擎、缓存与监控。
- `src/signals/`：信号策略、编排、过滤、评估与追踪。
- `src/trading/`：交易执行、准入、仓位与结果追踪。
- `src/persistence/`：数据库写入、队列持久化、仓储层。
- `src/backtesting/`：回测、优化、前推与参数推荐。
- `tests/`：测试套件（结构与 `src/` 对齐）。

## 4) 配置系统硬性规则

- **`config/app.ini` 是交易品种、时间框架、全局采集间隔的唯一事实源（SSOT）**。
- 禁止在其他 `.ini` 文件重复定义上述核心参数。
- 配置优先级（高→低）：
  1. `config/*.local.ini`
  2. `config/*.ini`
  3. 代码默认值（`src/config/centralized.py`）
- `.env` 已废弃；新增配置应优先进入 `.ini` 配置体系。

## 5) 信号参数配置约定

- 优先通过 `config/signal.ini` 调参数，避免直接改策略源码常量。
- 策略参数查找优先级：
  - `[strategy_params.<TF>]`
  - `[strategy_params]`
  - 策略代码默认值

## 6) 代码修改原则

- 先定位现有模块职责，再做最小必要改动，避免跨模块“顺手重构”。
- 保持与现有命名、分层、依赖方向一致。
- 新逻辑优先放在对应领域目录，避免“临时工具”散落。
- 默认禁止以“兼容补丁”“临时兜底”“额外分支特判”作为功能开发或优化的主要实现方式；应优先从领域边界、状态模型、模块职责出发，做可持续的工程化设计。
- 如需调整现有能力，优先重构到清晰模块或正式抽象中，再迁移调用方；不要为了短期兼容而长期保留双轨语义、别名字段或历史行为映射，除非用户明确要求兼容过渡。
- 涉及配置项新增时，需同时更新：
  - 配置加载模型（`src/config/`）
  - 默认配置文件（`config/*.ini`）
  - 必要文档/注释

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

- 若改动影响架构、流程或关键配置，需同步更新相关文档（如 `docs/architecture-flow.md`）。

## 10) Working Principles

You must reason from first principles. Start from the real objective, constraints, and root problem rather than conventions, templates, or the user's proposed path.

Rules:
1. If the user's goal, motivation, or success criteria are unclear, pause and clarify before proposing implementation.
2. If the user's requested path is not the shortest or most effective path, say so directly and propose a better one.
3. Always prefer root-cause analysis over surface-level fixes. Every non-trivial recommendation must answer: why this decision?
4. Keep outputs decision-focused. Include only information that changes action, architecture, risk, or tradeoffs.
5. Do not optimize for agreement. Optimize for correctness, clarity, and leverage.
6. If assumptions are necessary, state them explicitly and minimize them.

---

如与更深层目录中的 `AGENTS.md` 冲突，以更深层文件为准；如与系统/开发者/用户指令冲突，以后者为准。
