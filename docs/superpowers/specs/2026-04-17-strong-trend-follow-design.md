# StructuredStrongTrendFollow 策略设计

> 日期：2026-04-17
> 来源：FP.1 长窗口挖掘（12 个月，2025-04-17 ~ 2026-04-15）H1 rule_mining #5
> 作者：Claude（brainstorming 会话产出）

---

## 1. 背景

### 1.1 挖掘发现

12 个月挖掘在 H1 上产出高置信规则（`confidence: "high"`, `significance_score: 1.45`）：

```
IF adx14.adx > 40.12
   AND macd_fast.hist <= 1.61
   AND roc12.roc > -1.17
THEN buy
```

- train: 55.1% WR / n=621
- **test: 60.1% WR / n=143** ⭐
- H1 TF，rule_mining 自动分层：why=[adx14.adx, macd_fast.hist]，when=[roc12.roc]

### 1.2 语义解读

强趋势（ADX > 40）+ 动量回归中性（MACD hist ≤ 1.61）+ ROC 未崩溃（> -1.17）→ 顺势 buy 延续入场。

这是**"强趋势中动能温和回调后的延续入场"**形态，与经典"趋势市回调买入"吻合。

### 1.3 与 `regime_exhaustion` 的互补性

两者共享 ADX > 40 的强趋势前提，但方向判定**完全相反**：

| 策略 | 触发条件 | 方向语义 |
|------|---------|---------|
| `regime_exhaustion` | ADX > 40 **AND `adx_d3 <= 0`** + StochRSI 极端 | 趋势耗竭反转 |
| `strong_trend_follow` | ADX > 40 **AND `adx_d3 > 0`** + MACD 温和 | 趋势强势延续 |

`regime_exhaustion` 源码条件是 `if adx_d3 > 0: return False`（即 `adx_d3 <= 0` 通过）。新策略用**严格 `adx_d3 > 0`** 才通过，两者**在 `adx_d3 = 0` 临界点也保持互斥**（`= 0` 归 regime_exhaustion），无双开仓冲突。

---

## 2. 策略类定义

### 2.1 元数据

```python
class StructuredStrongTrendFollow(StructuredStrategyBase):
    name = "structured_strong_trend_follow"
    category = "trend_continuation"
    htf_policy = HtfPolicy.NONE              # 挖掘规则未使用 HTF
    preferred_scopes = ("confirmed",)        # H1，不接入 intrabar
    required_indicators = (
        "atr14",
        "adx14",
        "macd_fast",
        "roc12",
        "volume_ratio20",
    )
    regime_affinity = {
        RegimeType.TRENDING: 1.00,
        RegimeType.BREAKOUT: 0.60,
        RegimeType.RANGING: 0.00,
        RegimeType.UNCERTAIN: 0.20,
    }
    research_provenance_refs = ("2026-04-17-H1-rule-mining-#5",)
```

**理由**：
- `regime_affinity` 与 `regime_exhaustion` 一致——两者前提（强趋势）相同
- `htf_policy = NONE`——与挖掘证据一致，不加未验证假设

### 2.2 可调参数

```python
# 门槛
_adx_extreme: float = 40.0          # ADX 强趋势阈值
_adx_d3_min_strict: float = 0.0     # 与 regime_exhaustion 互斥：需严格 > 此值（adx_d3 = 0 归 regime_exhaustion）

# MACD hist 入场窗口（挖掘规则核心）
_macd_hist_upper: float = 1.61      # 挖掘阈值
_macd_hist_lower: float = -2.0      # 下限保护（防止深度崩盘中入场）

# ROC 保护
_roc_lower: float = -1.17           # 挖掘阈值

# BARRIER exit
_sl_atr: float = 1.5
_tp_atr: float = 2.5                # 趋势延续应吃到更多，略大于 regime_exhaustion 的 2.0
_time_bars: int = 20
```

---

## 3. 评估层实现

### 3.1 `_why()` — 硬门控 + 方向确认

```python
def _why(ctx):
    adx_data = self._adx_full(ctx)
    adx = adx_data["adx"]
    adx_d3 = adx_data["adx_d3"]
    plus_di = adx_data["plus_di"]
    minus_di = adx_data["minus_di"]

    if any(v is None for v in [adx, plus_di, minus_di]):
        return False, None, 0.0, "no_adx_data"

    # 硬门控 1：ADX 强趋势
    threshold = get_tf_param(self, "adx_extreme", ctx.timeframe, self._adx_extreme)
    if adx <= threshold:
        return False, None, 0.0, f"adx_low:{adx:.0f}"

    # 硬门控 2：多头方向（补挖掘规则缺失的方向判定）
    if plus_di <= minus_di:
        return False, None, 0.0, f"di_not_bullish:+{plus_di:.0f}/-{minus_di:.0f}"

    # 硬门控 3：ADX 严格上行（与 regime_exhaustion 严格互斥，adx_d3=0 归对方）
    if adx_d3 is None or adx_d3 <= self._adx_d3_min_strict:
        return False, None, 0.0, f"adx_not_rising:d3={adx_d3}"

    # 评分：ADX 越极端，趋势强度信号越强（40→0.4, 55→1.0）
    score = self._linear_score(adx, low=threshold, high=threshold + 15.0)
    score = max(score, 0.4)

    return True, "buy", score, f"strong_trend:adx={adx:.0f},+di>{plus_di - minus_di:.0f}-di"
```

### 3.2 `_when()` — 硬门控 + 入场时机

```python
def _when(ctx, direction):
    macd = ctx.indicators.get("macd_fast", {})
    roc_data = ctx.indicators.get("roc12", {})

    macd_hist = macd.get("hist")
    roc = roc_data.get("roc")

    if macd_hist is None or roc is None:
        return False, 0.0, "no_macd_or_roc"

    tf = ctx.timeframe
    hist_hi = get_tf_param(self, "macd_hist_upper", tf, self._macd_hist_upper)
    hist_lo = get_tf_param(self, "macd_hist_lower", tf, self._macd_hist_lower)
    roc_lo = get_tf_param(self, "roc_lower", tf, self._roc_lower)

    # 硬门控 1：MACD hist 上限（挖掘规则）
    if macd_hist > hist_hi:
        return False, 0.0, f"macd_hist_too_high:{macd_hist:.2f}"

    # 硬门控 2：MACD hist 下限保护（避免深度崩盘）
    if macd_hist < hist_lo:
        return False, 0.0, f"macd_hist_too_low:{macd_hist:.2f}"

    # 硬门控 3：ROC 未崩溃（挖掘规则）
    if roc <= roc_lo:
        return False, 0.0, f"roc_too_low:{roc:.2f}"

    # 评分：MACD hist 越接近 0 越"温和"，评分越高
    # hist=0 → 1.0, hist=hist_hi 或 hist=hist_lo → 0.0
    center = 0.0
    half_range = max(abs(hist_hi - center), abs(hist_lo - center))
    distance = abs(macd_hist - center)
    score = max(0.0, 1.0 - distance / half_range) if half_range > 0 else 0.0
    score = max(score, 0.3)

    return True, score, f"timing:macd_hist={macd_hist:.2f},roc={roc:.2f}"
```

### 3.3 `_where()` — 软加分（结构位）

```python
def _where(ctx, direction):
    ms = self._ms(ctx)
    # 与 regime_exhaustion 相同策略：压缩/突破状态加分
    if str(ms.get("compression_state", "none")) != "none":
        return 0.8, "compression"
    if str(ms.get("breakout_state", "none")) != "none":
        return 0.8, "breakout"
    return 0.0, ""
```

### 3.4 `_volume_bonus()` — 软加分（量能）

```python
def _volume_bonus(ctx, direction):
    return self._linear_score(self._volume_ratio(ctx), low=1.2, high=1.5)
```

### 3.5 `_entry_spec()` — 市价入场

```python
def _entry_spec(ctx, direction):
    return EntrySpec()  # 默认 MARKET
```

**理由**：强趋势延续是"追单"性质，等待回调到限价可能错过；规则 test 60.1% 已建立在市价入场假设上。

### 3.6 `_exit_spec()` — BARRIER

```python
def _exit_spec(ctx, direction):
    sl = get_tf_param(self, "sl_atr", ctx.timeframe, self._sl_atr)
    tp = get_tf_param(self, "tp_atr", ctx.timeframe, self._tp_atr)
    tb = int(get_tf_param(self, "time_bars", ctx.timeframe, self._time_bars))
    return ExitSpec(sl_atr=sl, tp_atr=tp, mode=ExitMode.BARRIER, time_bars=tb)
```

---

## 4. 配置（signal.ini）

### 4.1 `[strategy_timeframes]`

```ini
structured_strong_trend_follow = H1
```

### 4.2 `[strategy_deployment.structured_strong_trend_follow]`

```ini
[strategy_deployment.structured_strong_trend_follow]
deployment = paper_only
locked_timeframes = H1
research_provenance_refs = 2026-04-17-H1-rule-mining-#5
paper_shadow_required = true
```

### 4.3 `[regime_affinity.structured_strong_trend_follow]`

```ini
[regime_affinity.structured_strong_trend_follow]
trending = 1.00
breakout = 0.60
ranging = 0.00
uncertain = 0.20
```

### 4.4 实例绑定（`live_exec_a.local.ini` / `demo_main.local.ini`）

与 `regime_exhaustion` 同组部署——均为挖掘驱动 + paper_only + 反向互补。

---

## 5. 验收标准

### 5.1 回测门槛（12 个月，2025-04-17 ~ 2026-04-15）

| 指标 | 最低要求 | 期望 |
|------|---------|------|
| Sharpe | ≥ 1.5 | ≥ 2.0 |
| PF | ≥ 2.0 | ≥ 3.0 |
| MC p (Sharpe) | ≤ 0.05 | ≤ 0.01 |
| MC p (PF) | ≤ 0.05 | ≤ 0.01 |
| 笔数 | ≥ 50 | — |
| MaxDD | ≤ 15% | ≤ 10% |

### 5.2 回测命令

```bash
MT5_INSTANCE=live-main python -m src.ops.cli.backtest_runner \
    --environment live --tf H1 \
    --strategies structured_strong_trend_follow \
    --start 2025-04-17 --end 2026-04-15 \
    --monte-carlo \
    --json-output data/research/strong_trend_follow_validation.json
```

### 5.3 失败处理

- 若 Sharpe < 1.5 或 MC p > 0.05 → **不部署**，策略代码保留，config 状态设 `deployment = candidate`（不载入）
- 若 Sharpe ∈ [1.5, 2.0) 但 PF < 2.0 → **放宽参数重试**（如 `_adx_extreme` 降到 38 或 `_macd_hist_lower` 改到 -3.0）
- 若笔数 < 30（频率过低）→ 考虑 `_adx_extreme` 降到 35 以扩大样本

---

## 6. 约束与非目标

### 6.1 明确不做

- **不做 sell 方向**：挖掘仅验证 buy，sell 对称方向属于未验证假设
- **不接入 HTF**：挖掘规则未使用，保持证据一致性
- **不接入 intrabar**：H1 策略，`preferred_scopes = ("confirmed",)` 显式排除
- **不用 CHANDELIER trailing**：挖掘 test WR 是 barrier 语义下测出的，换 exit 模式就脱离证据

### 6.2 与 regime_exhaustion 的互斥保证

- 实现严格互斥：`strong_trend_follow` 要求 `adx_d3 > 0`，`regime_exhaustion` 允许 `adx_d3 <= 0`
- `adx_d3 = 0` 临界点归 `regime_exhaustion`，两策略不会在同一 bar 同时触发
- 若两者 metadata 上都出现 adx>40，coordinator 仍按 IntrabarTradeGuard 去重

---

## 7. 测试

### 7.1 单元测试（`tests/signals/strategies/structured/test_strong_trend_follow.py`）

覆盖：

1. ADX < 40 → hold（why_reason: `adx_low`）
2. plus_di ≤ minus_di → hold（why_reason: `di_not_bullish`）
3. adx_d3 ≤ 0 → hold（why_reason: `adx_not_rising`，与 regime_exhaustion 互斥；边界 `adx_d3=0` 也要拒绝）
4. macd_hist > 1.61 → hold（when_reason: `macd_hist_too_high`）
5. macd_hist < -2.0 → hold（when_reason: `macd_hist_too_low`）
6. roc ≤ -1.17 → hold（when_reason: `roc_too_low`）
7. 全部条件满足 → direction=buy，confidence ∈ [0.56, 0.90]
8. compression/breakout 状态 → where_score > 0 → signal_grade=A（若 volume_bonus>0）

### 7.2 集成测试

- 策略加载 + 目录注册可见性测试
- signal.ini [strategy_timeframes] 校验不中断启动

---

## 8. 部署计划

1. 实现策略代码
2. 目录注册 (`catalog.py`)
3. 配置文件更新（`signal.ini` + `live_exec_a.local.ini` + `demo_main.local.ini`）
4. 单元测试 + 集成测试
5. 回测验收（见 §5）
6. 若通过：commit + paper_only 部署
7. 若不通过：保留代码，config 设 `candidate`（不载入），总结问题
