# 2026-04-22 高频 TF 挖掘综合结果（4 experiments）

> **⚠️ 时点快照**：记录 **2026-04-22** 完成的 4-experiment 高频 TF 挖掘的关键发现 + Top Candidate 决策。
> **不回改正文**。后续策略实施 / 回测验证 / 二次挖掘均写新文件。
>
> **触发**：用户要求"挖掘高频 TF 策略 → 加入策略 → 一条龙回测"。
>
> **总耗时**：M1 backfill ~52s + 4 个 mining runs（A1 30min + A2 ~15min + B 8min + C 5min）≈ 60min

---

## 1. 4 个 experiment 配置 + 入库证据

| Experiment ID | Run | TF | Window | child_tf | n_bars 总 | sig 显著 | mined_rules |
|---|---|---|---|---|---|---|---|
| `exp_20260421_highfreq_close12mo` | A1 | M5/M15/M30 | 2025-04-21 ~ 2026-04-21 (12mo) | 无（close-only）| 105,100 | 17+6+3 | 20+17+8 |
| `exp_20260421_highfreq_intra_m1child` | A2 | M5/M15/M30 | 2026-01-07 ~ 2026-04-21 (3.4mo) | **M1** | 29,477 | 51+19+5 | 11+7+12 |
| `exp_20260421_h1_m5child_intra` | B | H1 | 12mo | M5 | 5,716 | **0** | 7 |
| `exp_20260421_h4_m15child_intra` | C | H4 | 12mo | M15 | 1,354 | **0** | 8 |

**持久化位置**：
- DB: `research_mining_runs` 表 8 行（所有 `full_result` JSONB 完整）
- JSON: `data/research/mining_20260421_*.json` 4 份（共 ~2.1MB）
- API: `GET /v1/research/mining` 列表 + `GET /v1/research/mining/{run_id}` 详情

---

## 2. 5 个关键结论

### 结论 1: 🔴 5 个 intra provider 特征全失败（**确定性结论**）

`child_bar_consensus / child_range_acceleration / intrabar_momentum_shift / child_volume_front_weight / child_bar_count_ratio` —— **所有 5 父 TF + 所有 4 experiment 都未进 top_10 / stable_ir**。

子 bar 数足够（M30 with M1 child = 30 子 bar，远超阈值 ≥4），但 IC 仍弱。这是 2 轮验证（2026-04-16 H1→M15 + 2026-04-22 全 TF）的结论性结果。

**对策略的影响**：**不写任何 intra 策略**。详见 MEMORY [intrabar_ic_validation.md](file:///C:/Users/Hugo/.claude/projects/d--MT5Services/memory/intrabar_ic_validation.md)。

### 结论 2: 🟡 H1 / H4 单 IC 测试 sig=0（反直觉但可解释）

B（H1 12mo）和 C（H4 12mo）的 `predictive_power.significant=0`，单特征 IC 全部不显著。但 H1 baseline 回测是 Sharpe 2.508 / PF 2.041 最强 TF。

**解释**：H1 的 alpha 来自**多特征组合**（rule_mining 决策树规则），不是单特征 IC。现有 `regime_exhaustion`（用 `bars_in_regime`）和 `strong_trend_follow`（用 `adx > 40` 组合）已挖到这部分。

**对策略的影响**：H1/H4 不需要新单特征策略；要补也是组合规则（rule_mining 输出）。

### 结论 3: 🎯 M30 `adx14.adx` IC=+0.145（A1 12mo，最强单特征）

A1 M30 的 `adx14.adx` IC=+0.145 / +0.125 / +0.119（不同 horizon），是**唯一稳定 IC > 0.1 的 close 特征**。

**现有覆盖**：`structured_strong_trend_follow`（`_adx_extreme=38`）已覆盖最强档。

**改进方向**：可考虑**多档 ADX 分级**（如 25/30/35/40 分级，区分弱/中/强趋势用不同 risk 倍数），作为现有策略的扩展。

### 结论 4: 🆕 M15 `squeeze20.squeeze_intensity` IC=-0.065（**新方向**）

A1 M15 的 `squeeze20` 反复入榜：IC=-0.065（×2）/ -0.058 / -0.035。Bollinger squeeze 强（vol 压缩）→ future return **反向**预测。

**现有覆盖**：❌ 无策略使用 `squeeze` 特征。

**对策略的影响**：**值得新写策略 `structured_squeeze_fade`**。Squeeze breakout 的 fade 信号是经典反转模式。

### 结论 5: 🆕 M5 `vwap_dev30.vwap_gap_atr`（**新方向**，多 TF robust）

A2 cross_tf_analysis 的 robust_signals 显示 `vwap_dev30.vwap_gap_atr` 在 M5/M15 同向（IC=-0.087 to -0.115）。与 VWAP 偏离过远（ATR 归一化）→ 回归。

**现有覆盖**：❌ 无策略使用 VWAP 偏离特征。

**对策略的影响**：**值得新写策略 `structured_vwap_reversion`**。

---

## 3. Top 3 Candidate（实施目标）

| # | 策略名 | TF | 核心特征 | 优先级 | 理由 |
|---|---|---|---|---|---|
| 1 | **`structured_squeeze_fade`** | M15 | `squeeze20.squeeze_intensity / squeeze20.squeeze` | 🥇 高 | A1 12mo 多次入榜，IC=-0.065 稳定，无现有策略覆盖 |
| 2 | **`structured_vwap_reversion`** | M5 | `vwap_dev30.vwap_gap_atr` | 🥈 中 | A2 cross_tf robust，A1 也出现，无现有策略覆盖 |
| 3 | **`strong_trend_follow` ADX 多档分级** | H1 / M30 | 现有特征加 25/30/35 多阈值 | 🥉 中（增量）| M30 adx14 IC=+0.145 印证，但是改进而非新策略 |

**所有候选默认 `deployment=paper_only`**，先 paper 跑 1-2 周再考虑 live binding。

---

## 4. 不推荐做的（明确清单）

| 候选 | 拒绝理由 |
|---|---|
| 任何 intra 策略 | 5 个 intra 特征 2 轮验证全失败（结论 1）|
| A2 M30 `williamsr14.williams_r` IC=-0.31 直接策略化 | 小样本 n=237，过拟合风险极大 |
| A2 M30 `momentum_consensus14` IC=-0.27 | 同上 |
| H1 / H4 单特征策略 | 单 IC sig=0（结论 2），需用 rule_mining 输出而非单 IC |
| `bar_stats20.close_position` 单独策略 | IC=-0.04 弱，更适合作为 confidence 加分项 |

---

## 5. 后续动作

### 立即（本 session 后）

- [ ] 实施 Top 1 `structured_squeeze_fade` 策略（M15，~60min 编码 + 测试）
- [ ] 跑回测验证 Top 1 是否真有 alpha（独立窗口，不挖掘窗口）
- [ ] 决定 Top 2 是否同步做 / 后做

### 中期

- [ ] 实施 Top 2 `structured_vwap_reversion`
- [ ] 改进 `strong_trend_follow` 加 ADX 多档分级
- [ ] 一条龙：停 paper → 全策略全 TF 回测 → WF 验证 → 决定 paper/live binding

### 长期

- [ ] **Phase R 性能优化**（已立项 docs/design/next-plan.md §Phase R），加速后续挖掘迭代
- [ ] 探索新 intra 特征（order flow / volume profile 等，不在当前 5 个失败特征上反复）

---

## 6. 引用 / 复现

```bash
# 重看 4 个 experiment（API）
curl http://127.0.0.1:8808/v1/research/mining

# 看某个 experiment 详情
psql ... "SELECT timeframe, full_result->'predictive_power'->'top_10'
          FROM research_mining_runs
          WHERE experiment_id='exp_20260421_highfreq_close12mo'"

# 直接读 JSON
cat data/research/mining_20260421_close12mo.json | jq '.results[] | {tf, sig: .predictive_power.significant}'

# 重新跑同样的挖掘（Phase R 优化前）
python -m src.ops.cli.mining_runner --environment live \
  --tf M5,M15,M30 --start 2025-04-21 --end 2026-04-21 \
  --compare --emit-candidates --emit-feature-candidates \
  --persist --experiment exp_<新ID> --workers 3
```
