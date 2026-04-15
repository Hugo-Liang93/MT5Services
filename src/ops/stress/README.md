# 压测/诊断脚本

用于量化数据流、intent 调度、存储通道在**真实生产数据**上的延迟与丢失分布。

## 原则

- 只读生产 DB / metrics，**不向真实交易链路注入流量**
- 输出分布（percentile / 直方图）而非单值——尾部才是量化决策的关键
- 不修改任何配置：脚本是"测量工具"，不是"修复器"
- 改参数前先用这些脚本建立当前基线

## 三脚本

### 1. `intent_latency_probe.py` — Intent 端到端延迟探针

**问题**：`[execution] poll_interval_seconds = 0.5` 是否真的吃掉了 25% 的紧信号窗口？
MT5 下单往返 + pre-trade checks 实际耗时多少？

**数据源**：`execution_intents` 表的 `created_at / claimed_at / completed_at`。

**输出**：publish→claim / claim→complete / publish→complete 三段延迟 p50/p95/p99/max。

```bash
python -m src.ops.stress.intent_latency_probe --days 3
python -m src.ops.stress.intent_latency_probe --days 7 --strategy breakout_follow --json
```

**如何读数**：
- `publish_to_claim.p99 >> 500ms` → poll 间隔是瓶颈，考虑降到 200ms 或改 LISTEN/NOTIFY
- `claim_to_complete.p95 > 2s` → MT5 下单 + pre-trade checks 有问题
- 按 TF 切分看是否某个 TF 信号被优先级挤压

---

### 2. `replay_intrabar.py` — `min_stable_bars` 参数扫描

**问题**：当前 `min_stable_bars=3` 意味 H1 最少 15min 才能盘中 armed。降到 2 能提前 5min 但会不会涨误触发率？

**数据源**：`signal_preview_events` 表（scope=intrabar 的策略输出历史）。

**做法**：按 (symbol, parent_tf, strategy) 分组、时序排序，重放到 `IntrabarTradeCoordinator`，扫描 N ∈ {2, 3, 4} 看 armed 次数和首次 armed 延迟。

```bash
python -m src.ops.stress.replay_intrabar --days 7
python -m src.ops.stress.replay_intrabar --days 14 --tf H1
python -m src.ops.stress.replay_intrabar --strategy breakout_follow --sweep 2,3,4,5
```

**如何读数**：
- N=3 vs N=2 的 `armed` 差值 = 提升稳定性挡掉的信号量
- `latency_from_bar_start_s.p50` 是"armed 平均延迟"，应显著小于父 TF bar 周期，否则 intrabar 价值存疑
- 结合回测 armed 信号的后续胜率（另做），决定 N 的性价比

**限制**：历史 preview 事件在评估时的 scope 可能与当前不同；若 intrabar_trading 刚开启不久历史数据稀疏。

---

### 3. `storage_saturation.py` — 队列饱和度时序采样

**问题**：突发 tick 涌入时（比如经济事件时刻）8 通道谁先饱和？有没有静默 drop？

**数据源**：轮询运行中的实例 `/v1/monitoring/queues` 端点。

**前置条件**：目标实例必须**运行中**。

```bash
# 快速采样（本机默认端口）
python -m src.ops.stress.storage_saturation --duration 60 --interval 1

# 长时间采样（10 min @ 每 5s）
python -m src.ops.stress.storage_saturation --duration 600 --interval 5 --json

# 指定实例端口
python -m src.ops.stress.storage_saturation --host 127.0.0.1 --port 8809 --duration 120
```

**如何读数**：
- `util_peak_pct` → 峰值压力（>80% = high）
- `drops_delta` 任意通道非零 → 存在静默丢失，需要定位时间窗口
- `status_counts` 里 `critical/full` 出现次数反映饱和持续时长

---

## 配合 `docs/codebase-review.md §2026-04-15` 使用

**本批脚本的作用是替代"拍脑袋给参数"**。2026-04-15 审计累计 9 处"代码里看起来是 bug"的误报，教训是 P0/FATAL 断言必须基于数据，不是直觉。`min_stable_bars` / `poll_interval` / overflow 策略等参数调整前，用这些脚本跑出当前分布，再决策。
