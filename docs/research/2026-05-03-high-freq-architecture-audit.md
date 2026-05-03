# 2026-05-03 高频策略基础设施架构 audit

承接 [2026-04-30 大清场](2026-04-30-day-trading-edge-strategies.md) (commit `0658fcc`)
后 catalog 仅剩 `structured_price_action`，下一轮要做"真正高频"day-trading 策略
（1+ trades/day 起步）。本文盘点当前框架对高频交易的支撑度、关键 gap、与
**为高频策略落地必须先解决的基础设施先决条件**。

## 评估维度

按数据 → 信号 → 执行 → 过滤 → 风控 → 延迟 → 回测真实度 → 持仓管理 → 可观测性
9 层评估。

---

## 1. 数据层

| 能力 | 当前状态 | High-Freq 适配度 | Gap |
|---|---|---|---|
| OHLC M1/M5/M15/M30/H1/H4 | ✅ TimescaleDB hypertable | ✅ | — |
| Tick / L1 quote stream | ❌ 不存储 | ❌ | broker MT5 API 不便取 tick；如要做 sub-bar 策略需自建 ticks 表 + ingestor |
| L2 / DOM | ❌ MT5 不开放 | ❌ | broker 限制，无解 |
| 跨品种 (DXY / 10Y / SPX) | ❌ 仅 XAUUSD | ❌ | 数据库 ingestor 改动 + symbol_table 扩展 |
| 历史长度 | 1y M15 / 1.1y H4 | ⚠️ | 高频策略要 walk-forward 验证至少需 2-3 年 |
| Volume 数据 | ✅ tick volume on bars | ✅ | XAUUSD 是 tick volume，非真实 volume |
| 经济日历 | ✅ EconomicCalendarService | ✅ | high-freq 策略对 news event 极敏感，已有 |

**结论**：bar-level (M5+) 已够用；tick / DOM / 跨品种均缺，意味着**真正的微观结构高频策略不可做**——可做的是"M5 bar-driven 高频"。

---

## 2. 信号生成层

| 能力 | 当前状态 | 适配度 | Gap |
|---|---|---|---|
| 35 个 indicator | ✅ 含 candle/bar_stats/price_struct/volume_ratio | ✅ | 高频策略可直接消费 |
| Intrabar 链路 | ✅ 子 TF close 驱动父 TF 合成 | ⚠️ | coordinator 要求 N 根稳定 bar = 内置延迟（高频不能等） |
| Per-tick event | ❌ | ❌ | runtime loop 是 bar-close 触发，不是 tick |
| Event-driven evaluation | ⚠️ confirmed 是 bar close 触发，intrabar 是子 TF close | ⚠️ | 最低延迟 = 子 TF bar 周期（M1=1min） |
| Indicator pipeline 性能 | ⚠️ 35 indicators × incremental compute | ⚠️ | M5 上每 bar 5min 重算 35 个 indicator，CPU bound 可能 |

**结论**：M5 bar-driven 高频可行，但需要确认 indicator pipeline 在 M5 / M1 频次下的 CPU 开销。Intrabar 链路若要用，需要降低 `min_stable_bars` 到 1（牺牲稳定性换响应）。

---

## 3. 执行层

| 能力 | 当前状态 | 适配度 | Gap |
|---|---|---|---|
| EntryPolicy 5 种 | ✅ market / pullback / breakout / oco / fib | ✅ | 高频默认 market 即可 |
| ExitMode 2 种 | ✅ BARRIER / CHANDELIER | ⚠️ | M5 ATR 不稳定，BARRIER fixed SL/TP 在 M5 上误差大 |
| Pending entry | ✅ PendingEntryManager | ⚠️ | 高频通常不用挂单；但 OCO 仍可用 |
| Pre-trade filter chain | ✅ 完整 | ⚠️ | 每 evaluate cycle 跑 4-5 个 filter，高频时累积开销 |
| MT5 API 下单延迟 | ⚠️ 同步 RPC | ⚠️ | 没测过 round-trip latency；怀疑 50-200ms |
| Slippage 模拟 | ⚠️ 固定 3pts | ❌ | 高频对 slippage 极敏感，需要时序化模型 |

**结论**：**MT5 同步 RPC 延迟是高频上限**——50-200ms round-trip 限制了"M1 真高频"的可行性，**M5 bar-driven 是合理上限**。

---

## 4. 信号过滤 / 质量层（ML overlay 新加入）

| 能力 | 当前状态 | 适配度 | Gap |
|---|---|---|---|
| Confidence pipeline 多层修正 | ✅ base + why/when/where/vol + affinity | ✅ | 高频可用；regime affinity 可降低假信号 |
| StateEdgeOverlay (ML 市场状态) | ✅ Research + Backtest 接入 | ❌ live 未接入 | live runtime 没消费 overlay；高频 ML 过滤未生效 |
| EntryMetaOverlay (ML 信号质量) | ✅ Research + Backtest 接入 + `runtime_safe` scope 已预留 | ❌ live 未接入 | 高频策略最需要的"过滤低质入场"能力没在 live |

**关键 Gap**：`EntryMetaOverlay` 的 `runtime_safe` feature scope + `dynamic_scoring_supported` 已设计但**没接入 live runtime**。这是高频策略的核心缺口——大多数高频策略原始信号 PF 低（如 1.0~1.2），靠 ML 过滤拉到 1.5+。

---

## 5. 风控层 (11 层 stack)

| 层 | 当前状态 | 高频影响 |
|---|---|---|
| SignalFilterChain (session/cooldown/spread/event/vol/trend衰竭) | ✅ | session_transition_cooldown 默认 15min — 高频时段会浪费机会 |
| Regime 门控 | ✅ regime_affinity 乘数 | ✅ |
| HTF direction alignment | ✅ 策略级分层 | ⚠️ 高频策略多数不需要 HTF |
| ExecutionGate (voting + intrabar 白名单) | ✅ | ✅ |
| PendingEntry | ✅ | 高频可绕过 |
| Pre-trade risk service (DailyLoss/Account/Margin/**TradeFrequency**) | ✅ | ⚠️ **TradeFrequency rule 默认 1/min**——高频时段会被拦 |
| Executor safety (技术熔断 + spread_to_stop) | ✅ | ✅ |
| ExposureCloseout 日终 | ✅ | ✅ |
| PositionManager + ChandelierExit | ✅ | ⚠️ chandelier trail 在 M5 上 ATR 噪音大 |
| IntrabarTradeGuard | ✅ | ✅ |
| RegimeSizing (TF 差异化) | ✅ | ✅ |

**关键 Gap**：`TradeFrequency` rule 默认 1 trade/min — 真正 high-freq (5+/min) 会被它拦。需要**调高 frequency cap 或改成 per-strategy/per-symbol cap**。

---

## 6. 延迟与性能

| 项 | 状态 | 高频影响 |
|---|---|---|
| 信号产生 → 下单 端到端 latency | ❌ 无测量 | 不知道 baseline |
| MT5 RPC round-trip | ❌ 无测量 | 推测 50-200ms |
| Indicator pipeline 单 bar 耗时 | ❌ 无测量 | 35 indicator × M5 = 5min/12 bars/h，CPU 应该够 |
| SignalRuntime 评估循环 | ⚠️ single-threaded + 分片锁 | M5 多策略时可能瓶颈 |
| Storage writer 队列 | ✅ 8 通道异步 | ✅ |
| Confirmed event 队列优先级 | ✅ confirmed > intrabar | ✅ |

**关键 Gap**：**没有 end-to-end latency tracking**。高频策略上线前必须测：
1. 子 TF close → SignalRuntime 评估 → ExecutionGate → MT5 send → fill 全链路 latency
2. 加 Prometheus / 内置 metric 看 P50/P99

---

## 7. 回测真实度

| 项 | 状态 | 高频影响 |
|---|---|---|
| EF mode lot 计算 | ✅ allow_min_volume_fallback 已修 | ✅ |
| Spread 模型 | ⚠️ session-aware 但固定 | ❌ 高频对 spread spike 极敏感，需时序化 |
| Slippage 模型 | ⚠️ 固定 3pts | ❌ 同上 |
| Commission 模型 | ✅ per-lot | ✅ |
| Partial fill | ❌ 不模拟 | ⚠️ XAUUSD 主流账户单笔 0.01 lot 不会 partial，影响有限 |
| Order rejection (高 spread / low margin) | ⚠️ 仅 max_spread cap | ⚠️ broker 真实拒单原因更多 |
| demo vs backtest 对账 | ✅ CLI 修复（commit 414c495）| ❌ 数据不足，等 demo 跑数据 |

**关键 Gap**：**spread / slippage / partial fill 模型固定**——高频策略 backtest PF 1.5 在 live 可能掉到 1.0。优先级：spread 时序化 > slippage 模型 > partial fill。

---

## 8. 持仓管理

| 项 | 状态 | 高频影响 |
|---|---|---|
| max_concurrent_positions_per_symbol | ⚠️ 默认 1 | ❌ 高频常需多仓位（不同 strategy / 不同方向） |
| 同方向重复入场 | ✅ IntrabarTradeGuard 去重 | ✅ |
| Hedge mode | ⚠️ Netting/Hedging 不明确 | ⚠️ 看 broker 账户类型 |
| Position scaling (加减仓) | ❌ | ⚠️ 高频部分策略需要金字塔加仓 |
| 持仓状态对账 | ✅ reconcile_interval=10s | ✅ |

**关键 Gap**：`max_concurrent_positions_per_symbol = 1` 是高频的硬约束。需放开到 3-5。

---

## 9. 可观测性

| 项 | 状态 | 高频影响 |
|---|---|---|
| HealthMonitor 内存环形缓冲 | ⚠️ ring_size=2400 仅 ~3.3h | ❌ 高频时段 24h 报告丢早期数据 |
| /v1/signals/runtime/status | ✅ 实时观测 | ✅ |
| Trade-level latency 指标 | ❌ | ❌ |
| Per-strategy fill rate | ⚠️ 数据库可查 | ⚠️ 没 dashboard |
| 推送告警 | ✅ Telegram 模块 | ✅ |
| Pipeline event bus | ✅ | ✅ |

**关键 Gap**：HealthMonitor ring_size 太小（高频时一小时就覆盖完）；trade-level latency 完全无观测。

---

## 综合 Gap 优先级（高频前必须解决）

### P0（不解决就无法可信跑高频）
1. **EntryMetaOverlay live runtime 接入**
   - artifact 已有 `runtime_safe` scope + `dynamic_scoring_supported=true`
   - 需要：`SignalRuntime` 注入 overlay 端口 + per-decision 调 `evaluate(...)` + block 时记录 metadata
   - 工作量：~2-3 天
2. **TradeFrequency rule 调整**
   - 当前默认 1 trade/min — 高频上限就是它
   - 需要：per-strategy / per-symbol cap，以及 high-freq 策略明确白名单
3. **Spread 时序模型 (回测真实度)**
   - 当前 session-aware 固定 spread 不够；高频需要 spread spike 模拟
   - 数据来源：demo 跑积累 spread tick history → 离线建模

### P1（有就更好，没有也能起跑）
4. **End-to-end latency tracking**：埋点 + Prometheus，先看 baseline
5. **`max_concurrent_positions_per_symbol` 放开到 3-5**
6. **HealthMonitor ring_size 提到 28800 (=24h × 12 bar/h × 100)**

### P2（高频策略稳定后再做）
7. **DOM / tick ingestor**（基础设施重投入）
8. **Slippage 时序模型**
9. **Position scaling (加减仓)**

---

## 决策建议（基于此 audit）

### 做高频策略前的 3 个先决条件

**先决条件 1 — EntryMeta live 接入（P0）**
没有它，高频原始策略 PF 1.1 上 live 就是亏。必须先做这个。

**先决条件 2 — TradeFrequency cap 调整（P0）**
否则任何 5+ trade/day 的策略在风控层就被拦。

**先决条件 3 — 1 个高频策略原型（DDV: design / dev / verify）**
- design：选 1-2 个 alpha 来源（推荐：volume spike + bar 极端，或 squeeze breakout）
- dev：写策略 + tests
- verify：跑 1y backtest，频次 ≥ 1/day + PF ≥ 1.0 才算"通过原型"
- 不通过 → 重新设计 alpha；通过 → 跑 EntryMeta lab 训练 ML 过滤

### 建议路径

```
Week 1-2: 写第一条高频策略原型 (M5 / M15)
          + 先决条件 1 (EntryMeta live runtime)
Week 2-3: 跑 baseline backtest → entry_meta_lab 训练
          + 先决条件 2 (TradeFrequency cap)
Week 3-4: forward shadow on demo
          + 先决条件 3 落地
Month 2+: 满足 PLAN.md gate (PF≥1.20, n≥80, DD≤15%) → demo_validation → active_guarded
```

### 不建议路径

- ❌ 跳过 ML 过滤直接 active：原始策略大概率 PF<1.2
- ❌ 投入 mining：上一轮已证明 mining-derived 策略 6/7 失败
- ❌ 改 PLAN.md gate 放水：没 alpha 时调阈值只是自欺欺人

---

## 当前 commit 与文档关联

- 大清场：`commit 0658fcc` (-3543 行)
- state_edge / entry_meta 设计与边界：`docs/codebase-review.md §0q / §0r` 历史记录
- entry_meta runtime_safe scope 设计：codebase-review §0r 2026-05-03 验证证据条目
- mining 框架冻结决策：`docs/research/2026-04-29-mined-rule-verification-pipeline.md`

## 留给下一轮 brainstorm

我准备好了下面的策略候选 + 数学预估（trades/day, PF 假设, alpha 来源），等你
拍板"先做哪一个高频策略原型"再展开 TDD 实施：

1. **Volume Spike Reversal (M5/M15)**：volume_ratio > 2 + 反向 candle → 反向交易
   预估 ~3-5 trades/day，alpha = 机构 sweep 后的反弹
2. **Bollinger Squeeze Breakout (M15)**：squeeze + 大 bar 突破 boll
   预估 ~1-3 trades/day，alpha = 波动率扩张
3. **Multi-Indicator Confluence Reversal (M15)**：rsi 极端 + bb 触轨 + pin
   预估 ~2-4 trades/day，alpha = 短期均值回归
