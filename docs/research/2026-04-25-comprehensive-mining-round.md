# 2026-04-25 周末全面挖掘 round

> 时点快照 — 不回改正文。  
> 数据来源：`data/research/p[12]*.json` + `p3_h1_walkforward.log`（live 库 XAUUSD）。  
> 触发原因：用户要求"全面科学的挖掘"，借此一并暴露挖掘模块问题。  
> 后续演进：进 backtest 验证 robust feature candidate 时再写新快照。

---

## 1. 范围与数据预检

| 维度 | 配置 |
|---|---|
| 品种 | XAUUSD（live 库） |
| TF | H4 / H1 / M30 / M15（D1 仅 258 bars 排除） |
| Intrabar 父子 | H1 父 × M5 子（覆盖 99.2%）；M30 父 × M5 子（覆盖 99.2%） |
| 12m 窗口 | 2025-04-24 ~ 2026-04-25 |
| 6m 窗口 | 2025-10-01 ~ 2026-04-25（对照段） |
| Walk-Forward | H1 切 4 个 3 个月窗口 |
| Providers | 全 6 个（temporal / microstructure / cross_tf / regime_transition / session_event / intrabar） |
| Persist | 全 6 run `--persist` 入 `research_mining_runs` |

---

## 2. 各 run 数据规模与 finding 概况

| run | window | TF | bars | PP_sig | TS_sig | rules*  | barr_sig | top_findings |
|---|---|---|---:|---:|---:|---:|---:|---:|
| 1A | 12m | H4  |  1352 |  2 | 0 | 0 |  4 | 15 |
| 1A | 12m | M15 | 23522 |  2 | 0 | 0 | 92 | 15 |
| 1B | 12m | H1  |  5734 |  0 | 0 | 0 |  5 | 15 |
| 1C | 12m | M30 | 11662 |  2 | 0 | 0 | 50 | 15 |
| 2A | 6m  | H4  |   869 |  0 | 0 | 0 | 16 | 15 |
| 2A | 6m  | M15 | 13279 |  6 | 0 | 0 | 41 | 15 |
| 2B | 6m  | H1  |  3321 | 22 | 0 | 0 |  1 | 15 |
| 2C | 6m  | M30 |  6640 |  4 | 0 | 0 | 16 | 15 |

`*` JSON `mined_rules.total = 0` 与 stdout 实际有 rule 输出冲突（见 §5 模块审查 #7）。

> **关注异常**：
> - 1B (H1 12m) PP 显著 = 0 但 2B (H1 6m) PP 显著 = 22 → 6m 窗口易出"小样本显著"  
> - threshold_sweeps 在所有 8 行全 0 → analyzer 判定门槛过严（见 §5 #6）  
> - barrier_PP 是主力 finding 来源（M15 12m 92 个！但需做后置 mean_return 过滤，见 §5 #1）

---

## 3. 跨窗口稳定性（核心成果）

### 3.1 Feature candidates — **12 robust / 27 unique**

| TF | feature | dir | kind | scope | live | 12m evid | 6m evid | 备注 |
|---|---|---|---|---|---|---:|---:|---|
| H1  | bars_in_regime | sell | computed | runtime_state | False | 2 | 1 | regime_transition |
| H1  | bars_in_regime | buy  | computed | runtime_state | False | 3 | 4 | 强证据双向 |
| H1  | body_ratio | buy | derived | bar_close | **True** | 2 | 2 | 可上 live |
| **H1** | **child_bar_count_ratio** | **sell** | **computed** | **bar_close** | False | 2 | 1 | **★ intrabar 派生跨窗口稳定** |
| H4  | bars_in_regime | buy | computed | runtime_state | False | 2 | 2 | |
| H4  | body_ratio | buy | derived | bar_close | **True** | 1 | 1 | 可上 live |
| H4  | momentum_consensus | buy | derived | bar_close | **True** | 4 | 1 | 可上 live |
| H4  | regime_entropy | buy | computed | runtime_state | False | 2 | 2 | |
| M15 | body_ratio | buy | derived | bar_close | **True** | 1 | 3 | 可上 live |
| M30 | bars_in_regime | buy | computed | runtime_state | False | 5 | 1 | |
| M30 | body_ratio | buy | derived | bar_close | **True** | 3 | 3 | 可上 live |
| **M30** | **child_bar_count_ratio** | **sell** | **computed** | **bar_close** | False | 1 | 3 | **★ intrabar 派生跨窗口稳定** |

**推荐进入 backtest 的优先级**：
1. **L1（live_computable=True，可直接接生产指标管线）**：`body_ratio` (H1/H4/M15/M30 buy)、`momentum_consensus` (H4 buy)
2. **L2（intrabar 派生，需带 child_tf=M5 接入）**：`child_bar_count_ratio` (H1/M30 sell)
3. **L3（runtime_state，需扩 SignalContext）**：`bars_in_regime` (H1/H4/M30)、`regime_entropy` (H4)

### 3.2 仅单一窗口出现（**慎用，疑似窗口 regime 现象**）

| 仅 12m | 仅 6m |
|---|---|
| H1 momentum_consensus buy | H1 body_ratio sell, child_bar_count_ratio buy, intrabar_momentum_shift sell |
| H4 bars_in_regime sell | H4 regime_entropy sell |
| M30 intrabar_momentum_shift sell | M15 bars_in_regime buy, momentum_consensus sell |
| M30 momentum_consensus buy | M30 bars_in_regime sell, body_ratio sell, child_bar_consensus buy, child_range_acceleration buy/sell |

### 3.3 Strategy candidates — **0 robust** (12m=6, 6m=21)

12m 与 6m 的 21 个 strategy candidate**没有任何一个交集**。说明：
- 单纯指标 IC/threshold 显著性时间不稳定
- 6m 窗口下 19 个 strategy candidate（其中 H1 占 11 个）→ 高度可能小样本 false positive
- **当前 strategy candidate 阶段产物不可作为 backtest 输入**

### 3.4 Walk-Forward 4 窗口 H1 — **0 stable rule**

```
Window 1 (2025-04~07): 9 rules
Window 2 (2025-07~10): 13 rules
Window 3 (2025-10~01): 7 rules
Window 4 (2026-01~04): 16 rules
Stable rules (≥2 windows): 0
"无稳健规则。所有挖出的规则均为单窗口样本波动，不应进 Paper。"
```

→ H1 上 rule_mining 类 finding 不具时间稳健性。**rule_mining 不应直接进 backtest**。

---

## 4. 重要发现（finding 层）

### 4.1 强 barrier signal（H4 12m，已验 mean_return > 0）

| indicator | dir | IC | barrier (sl/tp/time) | tp/sl/time | mean_ret | n |
|---|---|---:|---|---|---:|---:|
| adx14.adx | long | -0.462 | 2.5/5.0/120 | 38/62/0% | +0.09% | 304 |
| adx14.adx | long | -0.361 | 2.0/4.0/80  | 44/56/0% | +0.28% | 455 |
| momentum_consensus14 | long | +0.237 | 1.5/3.0/40 | 43/57/0% | +0.15% | 639 |
| momentum_consensus14 | long | +0.235 | 2.0/3.0/80 | 55/45/0% | +0.34% | 555 |

→ 这 4 条是当前 round **唯一已通过 mean_return 净筛**的 barrier 类强信号。其他 barrier "*** 显著"条目大多 mean_return < 0（详 §5 #1）。

### 4.2 M15 12m 强 rule（test 60%+，n>700）

```
#1 IF macd.hist > -3.75 AND volume_ratio20.volume_ratio <= 1.17
       AND momentum_accel14.roc_accel > -0.53 THEN sell
   train=72.8%/9692 test=64.7%/3594  ***

#2 IF macd.hist > -3.75 AND volume_ratio20.volume_ratio > 1.17
       AND cci20.cci <= 312.91 THEN sell
   train=64.5%/5306 test=60.5%/1753  ***

#3 IF di_spread14.di_spread > -0.40 AND adx14.adx <= 34.76
       AND macd_fast.hist <= 0.94 THEN sell  
   train=58.6%/7918 test=55.6%/2725  **
```

> **重大警示**：M15 没做 walk-forward，但 H1 walk-forward 已显示 rule 跨窗口零稳健。  
> 在把 M15 rule 进 backtest 前，**强烈建议先对 M15 单独跑 walk-forward**。

---

## 5. 挖掘模块审查发现（7 项）

### #1 [P1] barrier_predictive_power top_findings 不过滤 mean_return < 0

**症状**：`_rank_findings` 把 IC 显著的 barrier 全部进 top_findings，仅按 `|IC| × exit_skew` 排序。结果 H4 6m 出现 6 个 *** 级 sell barrier，但 mean_return 全部 -0.55% 量级。

**证据**：`p2a_close_h4m15_6m.json` H4 barrier `adx14.minus_di short IC=-0.460 sl=89% mean_return=-0.56%` 进入 top_findings #4。

**根因**：`src/research/orchestration/runner.py:476-520` 只过滤 `bp.is_significant`（IC p-value 显著），没检查 mean_return。action 文案有"亏钱——考虑反向或回避"提示，但排名等同处理。

**修复方向**：
- 选项 A：拆 top_findings 为 "Profitable barriers" / "Anti-pattern barriers" 两节
- 选项 B：默认门槛 `mean_return > round_trip_cost_pct`（0.08%），未过的 barrier 不入 top_findings 但保留在 `barrier_predictive_power` 详细字段

### #2 [P1] mining_walk_forward 缺失 mining_runner 多个核心能力

**症状**：`mining_walk_forward.py` 仅看 `mined_rules`，无法验证 feature_candidate / barrier 的跨窗口稳定性。也无 `--providers / --child-tf / --persist / --json-output`。

**影响**：Phase 3 H1 WF 0 stable rule 是真实结论，但 12 个 robust feature candidate 没法用 WF 二次验证。

**修复方向**：
- 把 walk-forward 升级为复用 `mining_runner._run_single`，输出 4 个 `MiningResult`，按 candidate / feature 维度做跨窗口聚合
- 同步 CLI 选项（providers/child-tf/persist/json-output）

### #3 [P2] BH-FDR `by_provider` 中 base 组失衡

**症状**：传统指标（adx14/rsi14/macd 等共 100+ 检验）汇成单个 "base" 组，与各 provider 组（5-30 个特征）共享 BH-FDR 流程。base 组阈值更严苛，provider 组更宽松。

**证据**：`p1b_h1_intrabar_12m.json` `findings_by_provider: {"base": 30}` 全归 base。

**修复方向**：把 base 组按指标族（momentum / volatility / trend / volume / structure）拆细，每族独立 FDR。

### #4 [P2] strategy_candidate 在 6m 窗口高 false positive

**症状**：6m 窗口出 21 个 strategy candidate，12m 出 6 个，**0 个交集**。6m 的高密度大概率是小样本 IC 显著膨胀。

**修复方向**：candidate emission 阶段加最小样本/IR 阈值，按 window 长度自适应。或在 candidate spec 上标注 `confidence_band` 让下游可见。

### #5 [P3] threshold_sweep analyzer 在所有 run 中 0 显著

**症状**：8 个 run × TF 行中 `threshold_sweeps.significant` 全为 0，但 mining_runner stdout 个别 run 列出了 threshold finding（如 1B `rsi14.rsi sell@51.58 hit=64.3%`）。

**疑因**：JSON 字段 `significant` 与实际 top_findings 进 threshold 类的判定路径不一致。或 expectancy + permutation_significance=0.05 + per_regime + n_permutations=200 组合极难通过。

**修复方向**：审查 `threshold` analyzer 的统计判定与 JSON 序列化字段口径，确认 `significant` 字段语义。

### #6 [P3] JSON `mined_rules.total = 0` 与 stdout 实际 rule 数不一致

**症状**：所有 8 行 `mined_rules` 字段为 0，但 1A M15 stdout 明显有 5 条 rule 进 top_findings。

**修复方向**：核对 `mined_rules` 序列化路径（可能字段名/类型变更但 dict export 没跟上）。

### #7 [P3] mining_runner ProcessPool 多 TF 并行模式 stdout 全部延迟到末尾

**症状**：`--workers > 1` 时所有 worker print 输出**直到全部 worker 完成才一次性输出**到主进程 stdout。期间 log 文件 0 字节，无法监控进度（1A H4+M15 跑 ~2h 期间 log 全空）。

**修复方向**：worker 内入口 `sys.stdout = sys.__stdout__`（spawn 默认 None），或主进程实时聚合（结合 logging stream handler）。当前设计使 `--workers > 1` 与"实时监控"互斥。

---

## 6. 模块审查 — 已正确的设计（撤回的初判）

- ~~ProcessPool 吞 stdout~~ → 实为延迟到末尾输出（#7），不是吞掉
- ~~feature_candidates 缺 source_provider~~ → `FeatureCandidateSpec` 本就没这个字段，源信息隐含在 `dependencies/runtime_state_inputs/compute_scope`

---

## 7. 接下来该做什么（推荐）

### 优先级 P0 — 模块修复（先于下一轮挖掘）
1. **#1 修复 barrier mean_return 过滤** — 否则任何 barrier finding 都不可信
2. **#2 升级 mining_walk_forward** — 否则没法对 12 个 robust feature 做时间稳定性二验

### 优先级 P1 — 进 backtest 验证
1. **L1 派生特征**：`body_ratio` 与 `momentum_consensus` 在 H1/H4/M15/M30 的 buy 方向（live_computable）
2. **L2 intrabar 特征**：`child_bar_count_ratio` 在 H1/M30 sell（需带 child_tf=M5）
3. **M15 12m 强 rule**（#1 - #3）：先对 M15 单独跑 walk-forward，存活的再 backtest

### 优先级 P2 — 数据补足
- **M1 数据扩充**：当前仅 3.5 个月（2026-01-07 起）。补 M1 后可启用 M5 父+M1 子 intrabar，覆盖 M5/M15 时段。
- **重启动态 backfill**：让 ingestor 在每周末加跑一次完整 D1/H4 历史校对（当前 D1 仅 258 bars）。

### 优先级 P3 — 不要做
- ❌ 不要把任何 strategy_candidate（6m 或 12m）直接进 backtest——0 跨窗口稳定。
- ❌ 不要把 H1 任何 mined_rule 进 paper——walk-forward 已证 0 稳健。
- ❌ 不要相信 H4 6m 的 6 个 *** sell barrier finding——mean_return 负值。

---

## 8. 附录：所有产出文件

```
data/research/
  p1a_close_h4m15_12m.{json,log}
  p1b_h1_intrabar_12m.{json,log}
  p1c_m30_intrabar_12m.{json,log}
  p2a_close_h4m15_6m.{json,log}
  p2b_h1_intrabar_6m.{json,log}
  p2c_m30_intrabar_6m.{json,log}
  p3_h1_walkforward.log
```

DB（`research_mining_runs` 表，env=live）：本轮共入 8 条记录（1A 入 2，1B/1C/2B/2C 各入 1，2A 入 2）。
