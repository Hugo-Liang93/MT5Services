# 2026-05-03 高频策略基础设施架构 audit

承接 [2026-04-30 大清场](2026-04-30-day-trading-edge-strategies.md) (commit `0658fcc`)
后 catalog 仅剩 `structured_price_action`，下一轮要做"真正高频"day-trading 策略
（1+ trades/day 起步）。本文盘点当前框架对高频交易的支撑度、关键 gap、与
**为高频策略落地必须先解决的基础设施先决条件**。

## 2026-05-04 Goal v1 决策更新

本轮实现把"高频"收敛为个人可实盘验证的 M1/M5 日内高频，不做 tick scalping：

- 交易主线：M1/M5 confirmed bar → indicators → `structured_micro_momentum` → EntryPolicy market → risk gates → execution intent
- intrabar：保留为空白增强支线；`enabled_strategies` 不再引用已删除策略
- tick：定义为数据资产，用于后续 spread/slippage 与 tick-derived feature；本轮不新增 tick-to-order 接口
- deployment：`structured_micro_momentum` 进入 `demo_validation`，只锁定 M1/M5 + london/new_york
- 风控：demo/live profile 分别在 `config/instances/demo-main/risk.ini`、`config/instances/live-main/risk.ini` 显式声明频率、仓位、手数、日亏损和 margin guard

## 评估维度

按数据 → 信号 → 执行 → 过滤 → 风控 → 延迟 → 回测真实度 → 持仓管理 → 可观测性
9 层评估。

---

## 1. 数据层

| 能力 | 当前状态 | High-Freq 适配度 | Gap |
|---|---|---|---|
| OHLC M1/M5/M15/M30/H1/H4 | ✅ TimescaleDB hypertable | ✅ | — |
| Tick / L1 quote stream | 数据资产，不进入触发链路 | ⚠️ | 本轮不新增 tick-to-order；后续只用于 spread/slippage 建模和 tick-derived 特征 |
| L2 / DOM | ❌ MT5 不开放 | ❌ | broker 限制，无解 |
| 跨品种 (DXY / 10Y / SPX) | ❌ 仅 XAUUSD | ❌ | 数据库 ingestor 改动 + symbol_table 扩展 |
| 历史长度 | 1y M15 / 1.1y H4 | ⚠️ | 高频策略要 walk-forward 验证至少需 2-3 年 |
| Volume 数据 | ✅ tick volume on bars | ✅ | XAUUSD 是 tick volume，非真实 volume |
| 经济日历 | ✅ EconomicCalendarService | ✅ | high-freq 策略对 news event 极敏感，已有 |

**结论**：M1/M5 bar-level 已作为个人日内高频 v1 主线；tick / DOM 不作为触发事实源，意味着**真正的微观结构高频策略不在本轮范围**。

---

## 2. 信号生成层

| 能力 | 当前状态 | 适配度 | Gap |
|---|---|---|---|
| 35 个 indicator | ✅ 含 candle/bar_stats/price_struct/volume_ratio | ✅ | 高频策略可直接消费 |
| Intrabar 链路 | ✅ 子 TF close 驱动父 TF 合成 | ⚠️ | coordinator 要求 N 根稳定 bar = 内置延迟（高频不能等） |
| Per-tick event | ❌ | ❌ | runtime loop 是 bar-close 触发，不是 tick |
| Event-driven evaluation | ⚠️ confirmed 是 bar close 触发，intrabar 是子 TF close | ⚠️ | 最低延迟 = 子 TF bar 周期（M1=1min） |
| Indicator pipeline 性能 | ⚠️ 35 indicators × incremental compute | ⚠️ | M5 上每 bar 5min 重算 35 个 indicator，CPU bound 可能 |

**结论**：M1/M5 confirmed-bar 高频可行，但需要确认 indicator pipeline 在 M5 / M1 频次下的 CPU 开销。Intrabar 链路若要用，必须先显式声明策略 scope 与白名单，再跑 `replay_intrabar` 验收。

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

**本轮处理**：不为制造频率放宽风控。`demo-main` 使用 `max_trades_per_day=20`、`max_trades_per_hour=4`、`max_positions_per_symbol=3`、`max_volume_per_symbol=0.03`；`live-main` 使用更窄的 active_guarded profile。quote freshness、spread、daily loss、margin guard 仍是硬门禁。

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
1. **M1/M5 confirmed-bar 原型与验收**
   - `structured_micro_momentum` 已接入 catalog / deployment / EntryPolicy / demo risk profile
   - 仍必须跑 1y/forward backtest，证明 `>= 1 trade/day` 且 PF 先过 1.0
2. **EntryMetaOverlay live runtime 接入**
   - artifact 已有 `runtime_safe` scope + `dynamic_scoring_supported=true`
   - 需要：`SignalRuntime` 注入 overlay 端口 + per-decision 调 `evaluate(...)` + block 时记录 metadata
   - 工作量：~2-3 天
3. **Spread 时序模型 (回测真实度)**
   - 当前 session-aware 固定 spread 不够；高频需要 spread spike 模拟
   - 数据来源：demo 跑积累 spread/tick history → 离线建模
4. **TradeFrequency profile 审计**
   - 本轮已有 demo/live 实例级 cap；若真实交易被频率规则误挡，再评估 per-strategy / per-symbol cap
   - 不能通过关闭 daily loss、spread、margin guard 来制造交易频率

### P1（有就更好，没有也能起跑）
5. **End-to-end latency tracking**：埋点 + Prometheus，先看 baseline
6. **`max_concurrent_positions_per_symbol` 放开到 3-5**
7. **HealthMonitor ring_size 提到 28800 (=24h × 12 bar/h × 100)**

### P2（高频策略稳定后再做）
8. **DOM / tick ingestor**（基础设施重投入）
9. **Slippage 时序模型**
10. **Position scaling (加减仓)**

---

## 决策建议（基于此 audit）

### 做高频策略前的 3 个先决条件

**先决条件 1 — M1/M5 原型先过 baseline（P0）**
不先证明 `structured_micro_momentum` 至少能达到 1 trade/day 且 PF > 1.0，后续 demo/live 都没有统计意义。

**先决条件 2 — EntryMeta live 接入（P0）**
没有它，高频原始策略 PF 1.1 上 live 就是亏。必须先做这个。

**先决条件 3 — 性能与风控基线（P0）**
至少跑 `intent_latency_probe` 与 `storage_saturation`，确认 execution intent latency 与 storage queue saturation 没有压垮 M1/M5 主线。

若 baseline 不通过 → 回到 alpha 设计；通过 → 跑 EntryMeta lab 训练 ML 过滤，再进入 demo 真实交易对账。

### 建议路径

```
Week 1:   structured_micro_momentum M1/M5 baseline backtest
Week 2:   intent/storage 性能基线 + entry_meta_lab 训练
Week 3-4: demo_validation 积累 20 笔真实交易
Month 2+: demo_vs_backtest 通过后，再进入 active_guarded 小资金 live
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

---

## Phase 0 Sanity (2026-05-03)

> 执行人：subagent Task 0.1，branch `feature/mining-feature-pool-expansion`
> 目标：在投入 Phase 1（CME volume + cross-asset）前，验证现有 ML pipeline 端到端可用。

### Step 1：research config 加载验证

```
feature providers enabled: ['temporal_enabled', 'microstructure_enabled',
  'regime_transition_enabled', 'session_event_enabled',
  'intrabar_enabled', 'candle_patterns_enabled']
```

6 个 provider 全部启用（temporal/microstructure/regime_transition/session_event/intrabar/candle_patterns）。

Baseline feature pool: 169 candidates (feature_manifest), 85 effective in H1 training (intrabar provider contributes 0 in H1 — no child-TF confirmed bars in mining window).

### Step 2：state_edge_lab H1 12mo 运行结果

- **退出码**：0（成功）
- **artifact 大小**：`artifacts/phase0_state_edge_h1_baseline.json` = 3,058 bytes（汇总）；
  per-TF artifact `artifacts/state_edge/state-edge-H1-20260503132356-a886e3/state_edge_artifact.json` = 2,076,333 bytes
- **数据覆盖**：H1 6,374 bars（2025-04-01 ~ 2026-04-30）
- **模型类型**：`hist_gradient_boosting`
- **标签分布**：long 3,092 / short 2,526 / no_trade 304
- **OOS samples**：1,656
- **OOS accuracy**：0.5103（基线约 51%，略高于随机）
- **高置信度桶 hit rate**：
  - short bucket：0.6211（threshold 0.740，样本 322）
  - long bucket：0.5759（threshold 0.553，样本 323）
- **特征池**：85 个有效特征（feature_manifest 报告 169 个候选，含 intrabar 0 个——H1 无子 TF 数据）

### Step 3：backtest_runner H1 运行结果

- **退出码**：1（失败，基础设施 bug）
- **错误信息**：`ValueError: No supported confirmed strategies remain after capability filtering`
- **根因**：`structured_price_action` 在 `signal.ini [strategy_timeframes]` 仅注册 `M15,M30`，
  H1 回测中无任何可用策略 → `BacktestEngine.__init__` 直接 raise 而非输出 0 trades 报告。
- **这是基础设施 bug**：runner 应在空策略集时优雅返回 0 trades 报告而非 crash。
  修复位置：`src/backtesting/engine/runner.py:444`，将 `raise ValueError(...)` 改为
  warning log + 返回空结果。
- **artifact**：未写入（crash 前退出）

### Verdict

**HALT and fix: backtest_runner crashes on empty strategy set (ValueError at runner.py:444)**

- state_edge_lab H1 12mo run：**PASS** — 退出码 0，artifact 完整，OOS accuracy 51%，
  short hit rate 62%（远高于随机）。ML pipeline 自身工作正常。
- backtest_runner：**FAIL** — `ValueError: No supported confirmed strategies remain`。
  这是 Phase 0 应该捕获的 infra bug。修复后重跑 backtest，再评估是否 PROCEED to Phase 1。

> **Note on Verdict tension**: plan §0 Step 3 originally allowed treating this crash as a separate-fix issue not blocking Phase 1. We chose the stricter `HALT and fix` interpretation because Phase 0's stated purpose ("ML pipeline end-to-end usable") is incomplete if backtest_runner can't run. Phase 1 implementer subagents should fix `runner.py:444` empty-catalog crash before Task 1.7 (mining run + report). Phase 1 Tasks 1.0 through 1.6 don't depend on backtest_runner so can proceed in parallel with the runner.py fix.

> 注意：此 bug 对 state_edge_lab 无影响（二者独立）。Phase 1 的 CME volume 数据源开发
> 可与 backtest_runner 修复并行，但 Phase 0 gate 严格要求 backtest_runner exit 0
> 才算通过。

---

## Phase 1 完成（2026-05-04）

> 执行人：subagent-driven flow，branch `feature/mining-feature-pool-expansion`
> 17 commits（含 Phase 0 的 2 个）+ 33 个新单测全 green，0 回归。

### Phase 0 阻塞修复

`src/backtesting/engine/runner.py:444` `raise ValueError` → warning log + 空 BacktestResult 早退路径（commit `008e75c`）。冒烟：`backtest_runner --tf H1 --start 2026-01-01 --end 2026-04-15` 现在 exit 0（原 exit 1），artifact `artifacts/phase0_baseline_h1.json` 写入，0 trades。`tests/backtesting/test_engine.py` 新增回归测试 `test_engine_returns_empty_result_when_no_strategies_match_tf`，11/11 通过。

### Phase 1 交付清单（Tasks 1.0–1.6）

| Task | 模块 | 测试数 |
|------|------|------|
| 1.0  | `src/research/external/protocol.py`（`ExternalDataSource` Protocol + `_REGISTRY`） | 5 |
| 1.1  | `pyproject.toml` 增加 `yfinance>=0.2.40` + mypy ignore | — |
| 1.2  | `src/persistence/schema/daily_external_ohlc.py`（Timescale hypertable） | 3 |
| 1.3  | `src/research/external/yfinance_client.py`（`YFinanceClient` + 自动注册） | 7 |
| 1.4  | `src/research/external/backfill.py`（通用 source-agnostic CLI + per-symbol commit） | 8 |
| 1.4.5| `src/research/features/external_data_lookup.py`（共享日线 lookup helper，lazy writer） | 6 |
| 1.5  | `src/research/features/cme_volume/provider.py`（`CMEVolumeFeatureProvider`，4 features） | 4 |
| 1.6  | `FeatureProviderConfig.cme_volume_enabled` + `FeatureHub` 注册 + `config/research.ini` | — |

抽象层落地（plan §"Extensibility Design Principles" 4 项全部实现）：
1. `ExternalDataSource` Protocol + `register_source()` registry
2. 通用 `backfill --source <name> --symbols <csv>` CLI
3. `make_daily_field_lookup(symbol, field)` 工厂（写一次，所有 daily-OHLCV provider 共用）
4. FeatureProvider 注册元数据驱动（`_PROVIDER_FACTORIES` + `<name>_enabled` 配置）

### Task 1.7 — Mining 运行结果

执行命令：
```bash
python -m src.ops.cli.mining_runner --environment live --tf H1 \
  --start 2025-04-01 --end 2026-04-15 \
  --providers temporal,microstructure,cross_tf,regime_transition,session_event,intrabar,candle_patterns,cme_volume \
  --child-tf M5 --no-auto-backfill \
  --json-output data/research/phase1_mining_h1_with_cme.json
```

| 指标 | with_cme（8 providers） | baseline（7 providers） |
|------|----------------------|-------------------------|
| `total_features` | 102 | 98 |
| `providers` 含 `cme_volume` | ✅（feature_count=4，elapsed=0.85s） | ❌（已正确排除） |
| `mined_rules_count` | 3 | 3 |
| `top_findings_count` | 15 | 15 |
| `predictive_power_count` | 4 | 4 |
| `threshold_sweeps_count` | 140 | 140 |

**结构验证（infra）**：✅ 完全通过——cme_volume Provider 注册成功、4 列特征注入 DataMatrix、mining 流水线无 crash、`--providers` flag 正确过滤（修了 `mining_runner.py:88-96` 的 `_ALL_PROVIDERS` 白名单遗漏 `cme_volume`，commit `<phase1_audit>`）。

**信号验证（alpha test）**：⏸️ **PENDING — 数据未入库**。`daily_external_ohlc` 表中 GC=F 行数 = 0；本地 IP 当天被 Yahoo Finance 限流（`YFRateLimitError: Too Many Requests`），`backfill --source yfinance --symbols GC=F` 多次尝试均返回 `{'GC=F': 0}`。`CMEVolumeFeatureProvider.compute()` 因 lookup 全返 None 输出全 None 列 → mining IC/FDR 跳过 → 0 cme_volume 规则被发现。with_cme 与 baseline 的 mined_rules / findings / threshold_sweeps 计数完全一致即由此而来。

**Verdict**: PROCEED to Phase 2（cross-asset DXY/10Y/SPX），但**先解决数据缺口**——优先级：

1. **必做**：Yahoo 限流恢复后（典型 24h）重跑 `backfill --source yfinance --symbols GC=F --start 2023-01-01`；验证表行数 ≥ 700 后重跑 with_cme mining 才可下"CME 是否提供 alpha"的判断。
2. **若 Yahoo 持续限流**：实现 Phase 2 的 `StooqClient`（用 `pandas_datareader` 抓 stooq.com 同源数据）作为 fallback——这恰好也是 plan §"Extensibility Design Principles" §1 抽象设计的目标场景，registry 切 `--source stooq` 即可，无需改 backfill / lookup / provider 任何代码。
3. **平行推进**：Phase 2 cross-asset Provider（DXY/10Y/SPX）的实现完全不依赖 GC=F 数据可用，可与限流恢复并行。Phase 1 的 4 个抽象层在 Phase 2 复用，预计仅添加 `CrossAssetFeatureProvider`（≈100 行）+ 4 行 hub 注册 + 1 行 `_ALL_PROVIDERS` 白名单。

### 已记录的技术债

- `make_daily_field_lookup` 当前硬编码 `_default_writer_factory = lambda: TimescaleWriter(load_db_settings("live"))`。demo/backtest 多环境调用需扩展签名引入 `environment` 参数。低优先级，单环境使用范围内可接受。
- `_ALL_PROVIDERS` 白名单与 `FeatureProviderConfig.<name>_enabled` 字段是手工同步的（添加新 provider 必须两处都改）。Task 1.6 的隐藏耦合教训——下一个 provider（cross_asset）也要记得同步。
