# 2026-04-18 挖掘 vs 回测 Gap 系统分析

> 关联：[ADR-007 Research 与 Backtesting 的职责边界](../design/adr.md#adr-007)
> 触发：Step 2.2 Top 1 规则 test hit rate 73.6%，真实回测 -1.71R/笔（零点差）。差距 60+ 个百分点。
> 本文档系统化"挖掘输出为什么不能直接当策略用"的根本原因。

---

## 一、现象汇总

2026-04-17~18 多轮验证给出了一致但反直觉的数据：

| 验证 | 挖掘显示 | 回测真实 | 差距 |
|------|---------|---------|------|
| **Step 2.2 Top 1 (M5)** | test hit 73.6% | 零点差 win 13.4% | 60 个百分点 |
| Step 2.1 M15/M30 | PP_sig 2/800 | Robust=0 | — |
| Step 2.2 M5/M15 | Robust=2, PP_sig 17/812 | 真实收益全亏 | — |
| Z 验证 body_ratio | 跨 TF Robust (buy 方向) | Q5 win 54.4% (< 55% 阈值) | 信号微弱 |
| X 验证 Top 1 as exit | — | fwd5 Δ=-0.0022%，10+/20 反转 | 仅短期有效 |
| H1 Intrabar 回测 | — | PF 0.757，3 策略 0 交易 | 基线亏损 |

这些数据**不是巧合**，而是挖掘管线与回测引擎之间存在**三类系统性 Gap**。

---

## 二、三类 Gap 的本质

### Gap 1：语义 Gap — "命中"定义完全不同

这是**最重要、最容易被忽略**的差异。

**挖掘的 `test_hit_rate`** 由 barrier 配置定义，默认是"**端点判断**"：
```
rule 触发（t=0）
  → 持有 20 bar（time_barrier）
  → 比较 close[t=20] 与 entry_price
  → 方向正确 = hit
```

**回测的 `win_rate`** 是"**路径判断**"：
```
rule 触发（t=0）
  → 按 SL=1.0×ATR, TP=1.5×ATR 开仓
  → 逐 bar 扫描 OHLC，任一时刻先触 SL 就亏损退出
  → 仅当先触 TP 才算 win
  → 或 time_barrier 到期，按 close 结算
```

**差异示例**（真实场景）：
```
sell 信号触发 @ $4800
  → t=5:  价格涨到 $4815（+15 USD）→ 回测触 SL 亏损退出（-1R）
  → t=10: 价格跌到 $4790（-10 USD）
  → t=20: 价格 $4785（-15 USD）→ 挖掘判 hit（方向正确）

同一信号：
  挖掘 = HIT（+0.3% 方向正确）
  回测 = LOSS（已在 t=5 止损出局）
```

**关键洞察**：**挖掘测的是"端点方向正确性"，回测测的是"路径生存性"**。

强牛市中 sell 信号：
- 短期漂移 +0.004%/bar × 20 bar = +0.08%，+SL 距离约 0.05%
- **大概率 5-10 bar 内先触 SL**，但 20 bar 后仍可能回到 sell 方向（挖掘算 hit）
- 这就是 Step 2.2 Top 1 挖掘 73.6% → 回测 13.4% 的 60 百分点差的主因

### Gap 2：成本 Gap — 挖掘零成本 vs 回测真实摩擦

| 维度 | 挖掘 | 回测 |
|------|------|------|
| 点差（entry） | 0 | 动态（亚 1.0 / 伦敦 1.2 / 纽约 1.3 × base 15 pts） |
| 点差（exit） | 0 | 同上 |
| 固定滑点 | 0 | 3.0 pts/侧 |
| 佣金 | 0 | $7/lot/侧 |
| Swap（过夜） | 0 | -0.3~+0.15 USD/lot/日 |

**成本占比（XAUUSD M5 场景）**：
- 典型 SL 距离 = 1.0 ATR ≈ $2-8/oz
- 每笔双边成本 ≈ $0.25-0.50/oz（低点差）或 $2-3/oz（默认配置）
- **成本占 SL 距离 5-50%**，即每笔 loss 从挖掘的 -1.0R 变成 -1.05R ~ -1.5R

### Gap 3：系统复杂度 Gap — 单信号 vs 完整管道

**挖掘**：每个特征/规则独立评估，只测 `feature → forward_return` 的直接相关

**回测（结构化策略）**：信号必须通过 11 层筛选

```
候选信号
  ↓
Strategy._why()           ← 方向确认评分（0~1）
Strategy._when()          ← 时机精度评分（0~1）
Strategy._where()         ← 结构位评分（0~1）
Strategy._volume_bonus()  ← 量能评分（0~0.05）
  ↓ 加权汇总
raw_confidence = 0.50 + why×0.15 + when×0.15 + where×0.10 + vol×0.05
  ↓
× effective_affinity (regime soft probs × strategy affinity dict)
  ↓
× performance_multiplier / calibrator (可选横切)
  ↓
× HTF_alignment (hard_gate/soft_gate/soft_bonus/none 策略级)
  ↓
final_confidence ≥ min_confidence (默认 0.45) ？
  ↓ 是
SignalFilterChain（session / cooldown / spread / volatility / economic）
  ↓
ExecutionGate（voting group 保护、intrabar 白名单）
  ↓
PendingEntry 价格确认 / 直接市价
  ↓
Pre-trade risk（daily loss / margin / frequency / trade guard）
  ↓
Executor safety（spread_to_stop ratio / 技术熔断）
  ↓ 全部通过
下单
```

**每层都是信号过滤器**。假设每层通过率 70%，11 层合计通过率 ≈ 3.5%。即使挖掘的原始信号 hit rate 73.6%，真正触发实盘下单的可能只有 3-5%。

---

## 三、数据验证：Top 1 规则的分层追溯

用 Step 2.2 Top 1 具体拆解：

```
挖掘层：
  rule 条件在 69550 bar 中命中 42524 次 (61% of bars)
  test set 中 20-bar 方向正确率 73.6%

验证层（我们的轻量模拟 rule_trade_frequency.py）：
  Level 0 raw hit      →  135.9/天（100%）
  Level 1 +cooldown    →  109.9/天（80.9%）
  Level 2 +session     →   60.3/天（44.4%）
  Level 3 +regime      →   40.6/天（29.9%）
  Level 4 +daily cap   →   15.9/天（11.7%）

真实回测层（rule_backtest_top1.py，零点差）：
  4816 trades
  SL 触发：4134 (85.8%) → 大部分 loss
  TP 触发：656  (13.6%) → 少部分 win
  time 触发：26  (0.5%)
  win_rate = 13.4%（而非挖掘的 73.6%）
  avg_loss = -2.10R（SL 距离 × 路径放大）
  avg_win = +0.81R（TP 距离 - 部分未触及）
  net expectancy = -1.71R/trade
```

**60 个百分点的差距**来自：
- ~30% 来自 Gap 1（端点 vs 路径判定，即使零点差也会差很多）
- ~20% 来自 Gap 2（点差吃掉原本小的 R 值）
- ~10% 来自 Gap 3（过滤后高质量场景可能反而更集中在不利时间）

---

## 四、系统设计含义

### 挖掘是"必要非充分条件"

```
有效特征存在 ⇏ 有效策略存在
```

挖掘找到 `IC > 0.05 且 p < 0.01` 的特征只是**起点**。从特征到可盈利策略还需要：
1. **路径稳健性验证**：在 SL/TP 时间 barrier 下的真实期望
2. **成本净化**：扣除动态点差后的净 EV > 0
3. **入场/出场 timing 细化**：挖掘的"进场时机"≠ 优化后的 entry_spec
4. **Regime 适应性**：挖掘的平均 IC ≠ 各 regime 下的条件 IC

### ADR-007 的 Tradability Filter 层是缺失的关键中介

当前管道：
```
Research 发现 → emit_candidates → (???) → 策略开发
```

ADR-007 提出应有的管道：
```
Research 发现 → emit_candidates → [Tradability Filter] → 策略开发
                                    ├─ 路径胜率验证（SL/TP 模拟）
                                    ├─ 成本净 EV 检查
                                    ├─ Direction 平衡检查
                                    ├─ Regime 分层 PF
                                    └─ 最小样本量门禁
```

今天的 Gap 分析为**Tradability Filter 的实现提供了具体规范**。

### 挖掘 metric 需要"actionable 化"

当前 `mining_runner` 输出的 metric：
- `hit_rate`（端点方向正确）
- `IC`（IC 与 forward_return 相关）
- `threshold_sweep.hit_rate`（阈值下命中率）

这些都是 **descriptive**。应当补充：
- `path_win_rate_sl1_tp15`（SL=1ATR, TP=1.5ATR 路径下的 win rate）
- `path_expectancy_zero_cost`（零点差下路径期望，以 R 为单位）
- `path_expectancy_typical_spread`（真实点差下路径期望）
- `direction_balance`（buy:sell 规则数量比）

---

## 五、本次发现的未解问题（移交下一阶段）

### 5.1 策略基线问题（更严重）

H1 Intrabar 回测显示 5 个启用的策略中 **3 个 0 交易**：
- `structured_trend_continuation`（配置 M30/H1，H1 上 0 交易）
- `structured_breakout_follow`（配置 M30/H1，H1 上 0 交易）
- `structured_range_reversion`（配置 M30 only，H1 上不跑是预期）

前两个是异常。已知：
- 默认 regime_affinity 合理（TRENDING=1.00, BREAKOUT=1.00 等）
- `deployment.status=paper_only`（不影响回测）
- `strategy_timeframes` 配置正确（M30,H1）

**未解**：为什么 H1 牛市 TRENDING（33%）+ BREAKOUT（30%）= 63% 有利 regime 下，两个核心策略 0 交易？

**假设**（待验证）：
- Why/When/Where 评分机制过严，raw_confidence 达不到 0.45
- 某个隐含 filter 在牛市中筛掉所有信号
- HTF alignment policy 在 H1 上过严

### 5.2 挖掘的 Sell-only 偏置

连续两轮挖掘（M15/M30 和 M5/M15）Top 10 规则 100% sell 方向。XAUUSD 12 月 +45.5% 牛市。

**未解**：
- 是否 rule mining 的贪婪搜索本身偏向 sell（例如 barrier 不对称）？
- 是否挖掘窗口太单一（只有牛市子集）？
- 是否需要按 regime 子集单独挖掘？

### 5.3 Intrabar 回测的指标缺失

H1 Intrabar 回测输出没有区分：
- confirmed 链路产生的交易
- intrabar_armed 链路产生的交易
- `total_intrabar_evaluations` / `total_intrabar_entries` 这些 intrabar.py 里定义的字段

**未解**：
- Intrabar 链路是否真的在这次回测中生效？
- 如何暴露 intrabar 贡献的细粒度指标到 backtest_runner 输出？

---

## 六、结论与下一步建议

### 今天得到的"不能用"的答案

**"挖掘效果好、回测不能用"** 的根本原因：

1. **Gap 1 命中定义**：挖掘的 hit 是端点方向，回测的 win 是路径存活 — 牛市中做空大概率先触 SL
2. **Gap 2 交易成本**：挖掘零成本 vs 回测真实点差 — 在 XAUUSD M5 上成本占 SL 的 5-50%
3. **Gap 3 系统复杂度**：挖掘单信号 vs 回测 11 层管道 — 通过率从 73% 降到 3-5%

### 下一步建议（优先级从高到低）

**P1 — 先解决策略基线问题**（2-4 小时）：
- 诊断为什么 `trend_continuation` / `breakout_follow` 在 H1 牛市下 0 交易
- 无效的策略基线上叠加 Intrabar / M5 新策略都是浪费

**P2 — 升级 Mining metric**（1-2 天）：
- 在 `mining_runner` 输出中补充 `path_win_rate` / `path_expectancy`
- 这相当于 ADR-007 提到的 Tradability Filter 的 Phase 1 实现

**P3 — 按 Regime 分层挖掘**（1-2 天）：
- 分别在 TRENDING / RANGING / BREAKOUT / UNCERTAIN 子集上跑挖掘
- 消除牛市 sell-only bias

**P4 — 重新审视 Intrabar 价值**（在 P1 完成后）：
- 只有当基线策略真的有 alpha 时，Intrabar 才有意义
- P1 验证基线有效后，才适合回到 Intrabar 链路验证

### 边界泄漏角度

本文档为**诊断型研究记录**，零代码改动。附带产出的 scratch 脚本（`rule_trade_frequency.py` / `rule_backtest_top1.py` / `validate_z_and_x.py` / `regime_switch_analysis.py`）均在 `.gitignore` 覆盖内，不入库。未决兼容项：无。
