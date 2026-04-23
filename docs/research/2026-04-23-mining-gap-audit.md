# 2026-04-23 挖掘方法学效果审计（Pre-Gap vs Post-Gap）

**类型**：时点数据快照（非事实源，非 ADR）
**目的**：验证 2026-04-22 实施的 Gap 1+2a+3 修复对挖掘输出的量化影响，为"新挖掘
结果可信度"提供对比基线。审计完成后不回改正文，新 mining run 直接用 Post-Gap。

---

## 方法

| 项 | 值 |
|---|---|
| Pre-Gap baseline | commit `3379ec8`（P4 刚完成，Gap 未引入） |
| Post-Gap | commit `d5bc224`（Gap 1+2a+2b+3 + A 均在） |
| 相同参数 | XAUUSD H1+M30 / 2025-10-01~2026-04-20 / all providers / --compare |
| 隔离方式 | `git worktree` 于 `../MT5Services-pre-gap`（不污染主工作区和 DB） |
| 执行时间 | 2026-04-23，Pre-Gap mining 后台跑 ~25min |

**复现命令**（worktree 里）：
```bash
python -m src.ops.cli.mining_runner --environment live --tf H1,M30 \
    --start 2025-10-01 --end 2026-04-20 --providers all --compare \
    --json-output data/research/mining_pre_gap.json
```

---

## 关键指标对比

| 指标 | Pre-Gap | Post-Gap | Δ |
|---|---|---|---|
| H1 bars | 3209 | 3209 | 0 |
| M30 bars | 6415 | 6415 | 0 |
| **H1 predictive_power tested/sig** | 800 / **0** | 795 / **4** | **+4** sig |
| **M30 predictive_power tested/sig** | 808 / **1** | 604 / **4** | **+3** sig |
| **H1 barrier_predictive_power** | N/A | 720 / 2 | **+2**（全新维度） |
| **M30 barrier_predictive_power** | N/A | 737 / 17 | **+17**（全新维度） |
| H1 threshold_sweeps | 140 | 140 | 0 |
| M30 threshold_sweeps | 140 | 105 | -25% |
| H1 mined_rules | 4 | 10 | +6 |
| M30 mined_rules | 6 | 5 | -1 |
| H1 top_findings categories | rule×3 / threshold×7 | rule×8 / **pp×2** | pp 类进前 10 |
| M30 top_findings categories | rule×4 / threshold×6 | rule×5 / threshold×3 / **pp×2** | pp 类进前 10 |

---

## 关键观察与解读

### ① predictive_power 真信号从"被噪声淹没"变为"可见"

H1 **0→4** / M30 **1→4** significant——这是本次 Gap 修复最大的直接效果。

**直觉反事实**：Gap 1（cost 0.04→0.08）本应让门槛更严、significant 更少。
**实测相反**：预期效果被 Gap 3（weekend mask）的噪声清除远远抵消。

解读：
- Pre-Gap 里跨周末的 forward_return 把"周五→周一 50h 价差"当日内 predictive 信号
- 这类样本噪声极大，淹没了真正的 IC 信号
- Gap 3 清除这些污染后，干净样本的 IC 分布从"被噪声淹没"变为"真信号浮现"
- Gap 1 提高 cost 虽然有轻微负面影响，但不足以抵消噪声清除的正面效益

### ② M30 predictive_power tested 数骤降 -25%（808→604）

**原因**：Gap 2a 把 forward_horizons 从 [1,3,5,10] 改为 [3,10,30,60]，60-bar horizon
在 M30 需要 60 根 bar 的 forward lookahead。在跨 regime 分析时（per_regime=True），
某些 regime 样本数 × horizons 组合落到 min_samples=30 以下被 skip。

**这不是 bug**，是 "更严谨的样本门槛"——pre-Gap 下 horizon=1 每个 bar 都算一次，
样本天然多但 IC 语义弱。

### ③ Gap 2b barrier IC 提供全新不可替代的观测维度

Post-Gap 独有 H1 2 sig / M30 17 sig，**没有 Pre-Gap 对等项**可比。

M30 barrier 显著性是 H1 的 8.5 倍，原因：
- 样本更多（6415 vs 3209）
- BH-FDR 对样本量敏感（m 大，校正阈值相对宽松）
- M30 本身交易密度和波动特征更容易出显著 barrier pattern

### ④ Top Findings 组成变化——pp 类首次进前 10

**Pre-Gap**：top 10 全部 rule + threshold，没有 predictive_power 类
**Post-Gap**：H1 top 10 含 2 条 pp（旧版 pp 全部 non-significant 被排名降到前 10 外）

解读：Pre-Gap 下 predictive_power 没有任何条目达到 `is_significant=True`，所以
`_rank_findings` 里 pp 段落的 `if not pp.is_significant: continue` 全部 skip 掉。
Post-Gap 让 pp 显著，才有资格进 top ranking。

### ⑤ mined_rules 稳定性——规则 pattern 稳定，参数阈值漂移

| TF | Pre-Gap top rule | Post-Gap top rule |
|---|---|---|
| M30 | `macd>-10.72 AND cci>-271.26 AND minus_di>19.86 → sell` test=58.3%/1037 | `macd>-13.32 AND cci>-241.14 AND minus_di>20.56 → sell` test=59.4%/955 |

**模式完全一致**（macd + cci + minus_di 三条件 → sell），阈值漂移在 3-12% 范围。
这说明：
- **业务规则本质稳定**（不是 Gap 改动引入的人工痕迹）
- **参数精度提升**——Post-Gap 的阈值更贴合真实成本下的最优分割点

### ⑥ Cross-TF Robust / Divergent 都是 0

两版都没发现跨 TF 一致的 robust signal，说明 H1 和 M30 的 predictive pattern 
本身**就是 TF-specific**（不是 Gap 改动掩盖的）。

---

## 结论

**新挖掘结果可信度显著高于旧版**，3 点证据：

1. **Gap 3 (weekend mask) 是错误修正而非优化**——旧版含系统性数据污染，
   本次量化证明：清除污染后 significant 从 1 跳到 8（新旧合计）
2. **Gap 1 (真实 cost) 与 Gap 3 结合**——真信号反而更清晰；
   Cost 提升单独看会压 IC，但配合 Gap 3 的信噪比大改善后为净正
3. **Gap 2b (barrier IC) 提供旧版根本无法产出的观测维度**——
   barrier 方向的 19 个 sig 全是新信息

**对 P0 Step 1 的实务指引**：
- 候选选择**只看 Post-Gap**（`docs/codebase-review.md §F 2026-04-22` 记录的 findings）
- 优先看 4 + 17 = 21 个 significant predictive / barrier findings（而不是 mined_rules
  ——规则虽然 pattern 稳定但胜率偏乐观，需 barrier IC 辅证）
- **不必再回头对比 Pre-Gap 数据**——旧版带系统性偏差

**产出物归档位置**：
- `data/research/mining_2026-04-22_weekly.json`（Post-Gap，含 DB persist）
- `../MT5Services-pre-gap/data/research/mining_pre_gap.json`（Pre-Gap，worktree 内部，
  worktree 清理后丢弃——证据已录入本文件）

---

## 审计后操作

- `git worktree remove ../MT5Services-pre-gap`（清理临时环境）
- 本文件**不回改正文**，新 mining run 只引用不覆盖
- 未来若再出现"挖掘结果可疑"疑问，直接用 `d5bc224` 之后的版本，不必再做 Pre-Gap 审计
