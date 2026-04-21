# TODO

> **📋 文档职责**（和兄弟文档的分工，新 session 开读前需理解）：
>
> | 文档 | 职责 |
> |---|---|
> | **本文（TODO.md）** | **当前 sprint 活跃待办**（可动作、本周~本月内推进）|
> | [`docs/design/next-plan.md`](docs/design/next-plan.md) | **长期技术方案**（Phase 级规划，跨 sprint）|
> | [`docs/codebase-review.md`](docs/codebase-review.md) | **已完成归档** + 风险台账 + 历史整改记录 |
> | [`docs/research/<日期>-*.md`](docs/research/) | **时点数据快照**（baseline 表、gap 分析等）|
> | [`CLAUDE.md`](CLAUDE.md) | AI 操作规则 + 配置 / 策略 / SOP 速查 |
>
> **内容迁移规则**（防止再次混乱）：
> - 任务完成 → 本文条目打勾；大块阶段完成后**整段移**到 `codebase-review.md §F` 归档
> - baseline / 回测快照 → **不写本文**，写 `docs/research/<日期>-<主题>.md`
> - Phase 级长期规划 → 写 `docs/design/next-plan.md`，本文只列"本 sprint 要推进的子任务"
>
> 按优先级分组。核心原则：**先验证再优化再上线**。

---

## 📍 当前状态（2026-04-21）

**阶段定位**：P0 结构化策略架构已完成（见 `codebase-review.md §F`），进入 **P1 Paper Trading 验证**。

**最新真实 baseline**（不再在本文维护，见 research/）：
- H1 baseline（P7 delta 修复后）：[2026-04-19-h1-p7-baseline.md](docs/research/2026-04-19-h1-p7-baseline.md) — 346 笔 / PF 2.041 / Sharpe 2.508，**唯一可投产 TF**
- 三 TF baseline review：[2026-04-20-tf-baseline-review.md](docs/research/2026-04-20-tf-baseline-review.md) — M30/H4 break-even，M30 price_action 污染事件

**paper 观察状态**（2026-04-21 快照）：
- session `ps_423ffdbf133f`（约 9h 运行）：余额 $1960.57（-1.97%），胜率 44.4%（4 胜 5 负）
- `structured_price_action` 历史累计 56 笔 / WR 14.3% —— **paper 数据明显偏离回测**（price_action deployment=paper_only 但 H1 baseline 里不含）
- 当前 3 个开仓（06:00 sell / 06:15 buy / 06:16 buy）

**冻结策略**（代码保留，通过 `regime_affinity=0.0` 冻结）：
- `structured_trend_continuation`：置信度反向校准（solo min_conf=0.45 → PF 0.61）
- `structured_lowbar_entry` / `structured_session_breakout`：入场质量不足

---

## P0: 结构化策略优化 — 待观察

> 架构工作已归档到 `codebase-review.md §F`。留待观察的数据项：

- [ ] M30 Monte Carlo p=0.057（接近 0.05 边界），需更长回测期或更多交易确认
- [ ] `breakout_follow` M30/H1 零交易是否设计如此（已二次证伪，但仍需 paper 数据对比）
- [ ] 频率瓶颈：~1.5 笔/周（H1 架构下），需通过新策略或放宽条件提频

---

## P1: Paper Trading 验证（进行中）

> 目标：在不冒资金风险的情况下验证系统端到端工作。
> 已完成的基础设施：`config/paper_trading.ini` + 双实例角色分工配置 + `TrackedPosition.initial_risk` bug 修复 + `config/exit.ini` 实例覆盖（见 `codebase-review.md §F`）。

- [ ] OBSERVE 模式运行 1-2 周，验证完整链路：信号接收 → 模拟执行 → 持仓管理 → 持久化
- [ ] 对比 Paper Trading 实际绩效 vs 回测预期（关注：成交率、滑点假设、信号延迟）
- [ ] 分别观察 live-main 与 live-exec-a 的出场行为：SL 是否真的在移动、两边 aggression 差异是否体现
- [ ] 如果 Paper 结果与回测差距 >30%，排查原因（过拟合？执行假设不成立？）
- [ ] **判定标准**：Paper 结果确认后才可进入 P2 实盘阶段
- 注意：multi-account 分工后预期频率 ~5-7 笔/周（main 低频 trend + worker-a 高频反转/突破）

---

## P2: 参数优化（Paper Trading 验证通过后）

### 2.1 Walk-Forward 前推验证

- [ ] 对结构化策略跑 Walk-Forward（3 个月 IS + 1 个月 OOS，滚动 4-6 窗口）
- [ ] 检查 overfitting_ratio：>1.5 的策略标记为不可信
- [ ] 检查 consistency_rate：<50% 的策略参数不稳定
- [ ] 用 `RecommendationEngine.generate()` 生成调参建议

### 2.2 Per-TF 参数精调

- [ ] 结构化策略 RSI 区间 / ADX 阈值 per-TF 网格搜索
- [ ] SL/TP ATR 倍数精调（特别是 M30 的 trailing stop 参数）
- [ ] `min_confidence` per-TF 调整（当前 M30=0.42, H1=0.40）
- 所有调参结果写入 `signal.local.ini`，不修改 `signal.ini`

---

## P3: 实盘试运行（参数优化完成后）

- [ ] 最小手数（0.01）+ 最保守风控参数
- [ ] `risk.local.ini` 设置：`daily_loss_limit_pct = 3.0`、`max_volume_per_order = 0.01`
- [ ] FULL 模式运行 1-2 周，每日检查：
  - 信号质量（`/signals/monitoring/quality/XAUUSD/M30`）
  - 交易审计（`/trade/command-audits`）
  - 持仓状态（`/account/positions`）
  - 日志文件（`data/logs/errors.log`）
- [ ] 确认无异常后逐步放大手数

---

## 🔴 未决工作（活跃未决，非规划）

### P5：breakout_follow 参数调优（低优先级，可选）

**状态**：原"参数调优"目标已被 bugfix 替代大部分效果（0 trades → 163 trades / PF 1.23）。

**仍可做**（非阻塞）：
- [ ] 让 `breakout_follow` solo PF 从 1.23 → 1.3+（减轻对整体 PF 的拖累）—— 参数网格 `_di_diff_min` / `_momentum_consensus_buy_min` / `_rsi_max_buy` / `_rsi_min_sell` 可扫
- [ ] 注意：`_adx_d3_min=1.0` 门槛在回测**不生效**（None 放行），在生产**生效**——参数调优结果在 live 行为可能更严

**前置**：建议先 1-2 周 Paper Trading 观察，对比回测 vs 实盘差异，再决定是否调优。

### P6：trend_continuation 置信度重设计（长，跨多 session）

**问题**：`raw_confidence = base + why×0.15 + when×0.15 + where×0.10 + vol×0.05` 与实盘胜率**负相关**——高 confidence 反而触发过拟合场景。

**候选方向**：
- [ ] 分析哪些 (regime, direction) 组合的高 confidence 对应低 WR
- [ ] 考虑加入 **in-sample confidence 再校准**（基于历史 bucket 的 calibration）
- [ ] 或重写 Why/When/Where 评分逻辑
- [ ] 目标：solo min_conf=0.45 时 PF > 1.2（当前 0.61）

### P8：回测注册与账户绑定对齐（架构性，中优先）

**问题**：`build_backtest_components()` 按 `signal.ini [strategy_timeframes]` 全量注册策略，不读 `signal.local.ini [account_bindings.*]`。后果：`deployment=paper_only` 且未绑定账户的策略（如 `structured_price_action`）在回测中被评估并计入统计，但生产中不跑。详见 [2026-04-20 tf-baseline-review](docs/research/2026-04-20-tf-baseline-review.md) §M30 污染事件。

**候选方向**（未决）：
- [ ] 方案 A：`BacktestConfig.respect_account_bindings: bool = False`，True 时只注册至少被一个 account_binding 引用的策略
- [ ] 方案 B：工具层固定传入 exclude 列表，明示哪些策略被排除
- [ ] 方案 C：在 `strategy_deployment.<name>` 合同里加 `visible_in_backtest: bool = true`

**前置**：次优先级。H1 baseline 已不受影响。

---

## 📋 下 sprint 候选

**P4 架构演进** / **Telegram Phase 2.5+** / **P5 前端 Dashboard** → 详见 [`docs/design/next-plan.md`](docs/design/next-plan.md)

---

## 历史归档

所有 `已完成` 段落已迁入 [`docs/codebase-review.md §F`](docs/codebase-review.md)。包括：
- 2026-04-20 P9 前端读侧 API 全套 + P10 QuantX 主控台闭环
- 2026-04-17 Telegram Phase 1 + Phase 2 + FP.2 strong_trend_follow
- 2026-04-14 Intrabar 交易链路
- 2026-04-06 架构清理 + 策略框架重构
- 2026-04-05 配置 & 基础设施清理
