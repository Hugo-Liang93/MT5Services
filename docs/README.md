# 文档导航

> 更新日期：2026-04-12
> 目的：为 `docs/` 提供单一入口，明确每份文档的职责边界，减少“同一事实在多处重复描述”的情况。

---

## 1. 如何使用这套文档

### 1.1 如果你的目标是判断“服务现在能不能跑”

按这个顺序读：

1. `docs/codebase-review.md`
2. `docs/design/full-runtime-dataflow.md`
3. `docs/design/entrypoint-map.md`
4. `docs/runbooks/system-startup-and-live-canary.md`

这 4 份文档负责回答：

- 当前已知阻塞项和未决风险是什么
- 启动顺序、数据流、日志路径、健康探针应该怎么看
- 服务该从哪里启动、启动后先看哪几个检查点
- 启动前预检、启动后 5 分钟巡检、休盘/开盘日志判读、开盘窗口 live canary 应该怎么执行

### 1.2 如果你的目标是改某个领域模块

按职责选专题文档：

| 目标 | 首选文档 |
|------|------|
| 系统分层、模块边界、目录归位 | `docs/architecture.md` |
| signals 运行时链路、状态流转、可观测性 | `docs/design/signals-dataflow-overview.md` |
| intrabar 支链 | `docs/design/intrabar-data-flow.md` |
| QuantX 执行页实时流与轮询优化 | `docs/design/quantx-trade-state-stream.md` |
| 策略开发规范、regime、单策略契约、TF 参数 | `docs/signal-system.md` |
| 审计规则、生命周期、私有属性边界 | `docs/design/adr.md` |
| signals / indicators / trading 模块化风险 | `docs/design/modular-audit-signals-indicators-trading.md` |

### 1.3 如果你的目标是看规划或历史方案

这些文档不是当前运行时事实源：

- `docs/design/next-plan.md`
- `docs/design/pending-entry.md`
- `docs/design/r-based-position-management.md`
- `docs/design/risk-enhancement.md`
- `docs/research-system.md`

---

## 2. 文档分层

| 类别 | 文档 | 约束 |
|------|------|------|
| 当前实现真相 | `architecture.md`、`codebase-review.md`、`design/full-runtime-dataflow.md`、`design/signals-dataflow-overview.md`、`design/intrabar-data-flow.md`、`design/entrypoint-map.md`、`design/quantx-trade-state-stream.md` | 用于判断当前代码与真实启动状态；涉及运行时结论必须与代码和验证结果一致 |
| 运维 Runbook | `runbooks/system-startup-and-live-canary.md` | 用于执行启动前检查、启动后巡检和开盘 canary，不重复定义新的事实源 |
| 审计与治理 | `design/adr.md`、`design/modular-audit-signals-indicators-trading.md`、`design/high-risk-remediation-milestones.md` | 用于说明约束、边界、整改顺序，不承担运行时状态播报 |
| 领域设计参考 | `signal-system.md` | 用于说明模块内部设计、策略接口、参数语义；不负责启动排障 |
| 规划 / 历史方案 | `design/next-plan.md`、`design/pending-entry.md`、`design/r-based-position-management.md`、`design/risk-enhancement.md`、`research-system.md` | 用于方案讨论、规划和历史设计记录，不应直接当作“当前已上线行为” |

---

## 3. 收口规则

为避免冗余，后续维护按下面规则执行：

1. `architecture.md` 只保留系统分层、边界、依赖方向和文档入口，不重复展开信号策略、回测参数、运行时验证细节。
2. `full-runtime-dataflow.md` 只保留运行时事实、启动顺序、日志路径、健康探针和当前验证结论，不重复讲策略开发规范。
3. `signals-dataflow-overview.md` 只保留 signals 域的运行时链路、状态流转和观测点，不重复展开策略开发规范与长篇配置说明。
4. `signal-system.md` 只保留策略开发、regime、单策略执行契约、TF 参数和领域规则，不承担启动排障职责。
5. 规划类文档只能描述目标方案、未决设计和演进路线，不能混入“当前已经验证”的口径。

---

## 4. 统一格式约定

1. 运行时流图统一使用 Claude 风格 ASCII，不使用 Mermaid。
2. 涉及“当前状态 / 已验证 / 已修复”的文档，必须带日期，并能对应到真实代码或测试/启动验证。
3. 新增文档前先判断能否并入现有专题，避免用新文件复制已有结论。
4. 同一条运行时事实只允许有一个主文档作为权威来源，其他文档一律用链接引用，不再重复全文描述。
