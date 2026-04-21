# 文档导航

> 更新日期：2026-04-21
> 目的：为 `docs/` 提供单一入口，明确每份文档的职责边界 + md 管理规范，避免"同一事实在多处重复"与"维护错过时 md"。

---

## 1. 如何使用这套文档

### 1.1 目标是判断"服务现在能不能跑"

按顺序读：

1. `docs/codebase-review.md` — 最新风险台账 + 变更历史
2. `docs/design/full-runtime-dataflow.md` — 运行时数据流事实源
3. `docs/design/entrypoint-map.md` — 入口映射
4. `docs/runbooks/system-startup-and-live-canary.md` — 启动巡检

### 1.2 目标是改某个领域模块

| 目标 | 首选文档 |
|------|------|
| 系统分层 / 模块边界 / 目录归位 | `docs/architecture.md` |
| signals 运行时链路 / 状态流转 | `docs/design/signals-dataflow-overview.md` |
| intrabar 支链 | `docs/design/intrabar-data-flow.md` |
| 策略开发规范 / regime / 单策略契约 / TF 参数 | `docs/signal-system.md` |
| Research 挖掘 / Feature Providers / 晋升通道 | `docs/research-system.md` |
| **持仓管理**（peak/reconcile/出场一致性）| `docs/design/position-state-consistency.md`（已实现）+ `r-based-exit-plan.md`（规划）+ `pending-entry.md`（历史）|
| **风控**（PnL 熔断 / 11 层堆栈）| `docs/signal-system.md §5.5`（当前权威）+ `design/risk-enhancement.md`（设计演进）|
| 回测 vs 实盘差异 / 挖掘 gap | `docs/design/backtest-vs-live-divergence.md` + `docs/research/2026-04-18-mining-vs-backtest-gap.md` |
| QuantX 执行页 SSE 流 | `docs/design/quantx-trade-state-stream.md` |
| QuantX 数据时效性分级与推送通道 | `docs/design/quantx-data-freshness-tiering.md` |
| QuantX 规范 IA 模型 | `docs/design/quantx-canonical-ia.md` |
| 审计规则 / 生命周期 / 私有属性边界 | `docs/design/adr.md` |

### 1.3 目标是看规划或历史方案

- `docs/design/next-plan.md` — 下一阶段规划
- `docs/design/pending-entry.md` / `r-based-position-management.md` / `risk-enhancement.md` — 领域设计参考
- `docs/research/<YYYY-MM-DD>-*.md` — 时点研究快照（**不回改**，只加"后续演进"头注）
- `docs/superpowers/plans/<YYYY-MM-DD>-*.md` / `specs/...` — skill 规划/规范（一对配套；完成后移出）

---

## 2. 文档分层（唯一合法位置）

```
docs/
├── README.md           ── 导航（仅此一份，禁止在此层另建总览）
├── architecture.md     ── 事实源：系统分层、边界、依赖方向
├── signal-system.md    ── 事实源：策略领域
├── research-system.md  ── 事实源：Research 域
├── codebase-review.md  ── 审计台账（每次大变更追加 §N）
├── quantx-*.md         ── 对前端的稳定契约
├── TODO / 下一阶段 / 领域设计 —— 合并到此层不新建顶层 md
├── design/
│   ├── adr.md          ── ADR 唯一入口（所有架构决策都在这里，不拆文件）
│   └── <topic>.md      ── 领域设计（长期有效）
├── research/<YYYY-MM-DD>-<topic>.md  ── 时点快照（不回改，过期靠"后续演进"头注识别）
├── runbooks/<topic>.md ── 运维操作手册
├── sop/<topic>.md      ── 流程手册（如特征晋升）
└── superpowers/        ── skill 规划（plan/spec 一对；完成后 archive 或移到 design/）
```

| 类别 | 约束 |
|------|------|
| 事实源（architecture / signal-system / research-system + design/full-runtime-dataflow 等）| 必须与代码实时对齐；涉及运行时结论要能对应真实代码 |
| 审计治理（codebase-review / adr）| 每次变更追加段落；不删旧段落（用时间顺序归档） |
| 运维 Runbook | 操作步骤；不新造事实（事实源归架构文档）|
| 领域设计 | 长期有效的设计方案；可标"已实现 / 拟定 / 演进方向" |
| 研究快照 `research/<日期>-*` | **不回改**，只加头注指引最新状态 |
| Superpowers plan/spec | 一对配套出现；skill 上线后 plan 移除或归档 |

---

## 3. md 管理规范（**新建 md 前必须回答**）

### 3.1 新建前四问

```
1. WHERE 归哪一层？（必须落到上表 5 类之一）
2. WHY 为什么不能写进现有 md？（先搜现有 md，避免零碎）
3. WHEN 生命周期？（长期维护 / 时点快照 / 临时过渡）
4. WHO 负责维护？（作者 + 下次更新触发条件）
```

**任何一问答不出来就不要建**。时点性分析往 `research/<日期>-<主题>.md` 或 `codebase-review.md §N` 写；架构决策往 `design/adr.md` 追加 ADR 编号；不要在 docs/ 顶层或 design/ 乱建新文件。

### 3.2 禁止清单

- ❌ 在项目根目录新建业务 md（仅允许 `CLAUDE.md` / `AGENTS.md` / `README.md` / `TODO.md`）
- ❌ 在 `docs/` 顶层新建非 5 类归属的 md
- ❌ 文件名带 `-old` / `-legacy` / `-draft` / `-wip` / `-v2`（用 git 历史，不用后缀版本）
- ❌ 把时点分析（"2026-04-xx 事故"）写进事实源（`architecture.md` 等）—— 事故要追加到 `codebase-review.md §N` 或 `research/<日期>-<事故>.md`
- ❌ 创建"同主题平行 md"（已有 `design/pending-entry.md` 就不要再建 `design/pending-entry-v2.md`）

### 3.3 维护错 md 的预防

- **commit 触发 SOP**（强制；CLAUDE.md §12 已有，这里是扩展清单）：
  | 变更类型 | 必改文档 |
  |---|---|
  | 架构改动 | `architecture.md` + `adr.md`（若属决策） |
  | 策略/指标数量变化 | `CLAUDE.md §项目概览` + `signal-system.md` |
  | Research 模块变更 | `research-system.md` + `codebase-review.md §N` |
  | API 契约变化（影响前端） | `quantx-canonical-ia.md` + OpenAPI 契约同步 |
  | 事故/大改 | 追加 `codebase-review.md §N`（不删旧段落）+ 必要时新建 `research/<日期>-<事故>.md` |
  | 时点研究结论 | 写 `research/<日期>-<主题>.md`，**不改事实源** |

- **时点快照的正确保鲜方式**：不改正文，加"后续演进"头注链到最新决策 / commit（示例见 `docs/research/2026-04-18-mining-vs-backtest-gap.md`）。

### 3.4 合并规则

- 同一运行时事实只有一个权威主文档，其他用链接引用，不重复全文
- 规划文档只描述目标方案，不写"当前已经验证"（那是事实源的工作）
- 事实源必须带日期，并能对应到 commit hash / 测试文件 / 启动日志

---

## 4. 统一格式

1. 运行时流图用 Claude 风格 ASCII（不用 Mermaid，保持纯文本可 grep）
2. "已验证 / 已修复 / 已落地"必须带日期 + commit hash
3. ADR / codebase-review 里的段落编号只追加、不重排（保持历史可追）
4. 非英文正文允许，但代码名、文件路径、配置键保持英文原样

---

## 5. 文档健康检查清单

维护者每月可自查：

```
□ docs/ 顶层是否有不归 5 类的 md？  → 归类或删
□ research/ 下有没有 > 60 天的快照缺"后续演进"头注？ → 补或删
□ superpowers/ 下有没有"skill 早已上线但 plan/spec 还在"的孤儿文档？ → 归档或删
□ ADR 标"拟定中"超过 1 个月？       → 升级为"已确定"或"废弃"
□ design/ 下有没有相近主题重复 md？  → 合并
□ TODO.md 里的 P 编号段落是否已完成但未标记？ → 归档
```

---

**本文件本身的维护规则**：本 README 是"如何管理 md 的 md"，只允许在"新增分层 / 改变规范"时更新，不追踪日常内容变化。改动前问自己"是不是真的多了一类 md 要归？"——如果不是，改具体文档即可。
