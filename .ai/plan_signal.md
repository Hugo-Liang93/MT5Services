# Signal Module — Optimization Plan & Issue Disclosure

Last updated: 2026-03-22

---

## 一、当前架构健康度

### 配置化完成度

| 层级 | 已配置化 | 未配置化（硬编码） |
|------|---------|------------------|
| Regime 检测 | adx_trending/ranging, bb_tight_pct | soft_regime 内部评分权重（算法常量，不应暴露） |
| 置信度流水线 | min_affinity_skip, confidence_floor | — |
| 策略阈值 | RSI 30/70, WR -80/-20, CCI ±100, StochRSI 20/80, ADX 门槛, ROC 阈值, session ATR 阈值 | 置信度公式中的缩放系数和加成值（见下方 Backlog） |
| Regime 亲和度 | 全部 20 个策略均可通过 `[regime_affinity.*]` 覆盖 | — |
| 投票引擎 | threshold, quorum, penalty, voting_groups | — |
| 执行层 | HTF 惩罚/加成, 熔断器, 仓位限制 | — |
| 绩效追踪 | baseline, min/max_multiplier, streak | — |
| Calibrator | alpha, min_samples, recency | — |

### 数据流验证结果

| 配置链路 | 状态 | 说明 |
|---------|------|------|
| INI → SignalConfig → MarketRegimeDetector | ✅ 通畅 | `[regime_detector]` section 三个阈值完整传递 |
| INI → strategy_params → 策略属性 | ✅ 已修复 | 4 个均值回归策略 + session 策略已添加 `__init__`，`setattr` 可生效 |
| INI → regime_affinity_overrides → 策略 | ✅ 通畅 | 字符串键正确映射到 RegimeType 枚举 |
| `[regime]` vs `[regime_detector]` | ✅ 无冲突 | 两个 section 前缀分离，不会互相覆盖 |
| Hot Reload | ⚠️ 部分 | regime_detector 和 strategy_params 不支持热加载（需重启），其余已支持 |

---

## 二、已发现并修复的问题

### Fix-1: 5 个策略缺少 `__init__` 导致配置无效（已修复 ✅）

**问题**：`config/signal.ini` 的 `[strategy_params]` 通过 `setattr(strategy, "_param", value)` 覆盖策略属性，但以下 5 个策略没有 `__init__` 方法，属性不存在，`hasattr()` 检查返回 False，配置被静默忽略：

- `RsiReversionStrategy`：RSI 30/70 硬编码在 `evaluate()` 中
- `WilliamsRStrategy`：%R -80/-20 硬编码
- `CciReversionStrategy`：CCI ±100 硬编码
- `StochRsiStrategy`：K 20/80 硬编码
- `SessionMomentumBias`：ATR 阈值硬编码

**修复**：为每个策略添加 `__init__`，将硬编码值改为 `self._xxx` 实例属性引用。

**涉及文件**：
- `src/signals/strategies/mean_reversion.py`（4 个策略）
- `src/signals/strategies/session.py`（1 个策略，此前已修复）

### Fix-2: `0 or default` falsy 陷阱（已修复 ✅ — 前序会话）

**问题**：`mt5_trading.py` 和 `mt5_market.py` 中 `int(getattr(info, "trade_mode", 4) or 4)` 当 `trade_mode=0`（DISABLED）时被 Python falsy 规则误读为 4（FULL）。

**修复**：改用 `if val is not None` 判断，添加 `_int_field()` 辅助方法。

### Fix-3: `precheck_trade` broker checks/warnings 未合并（已修复 ✅ — 前序会话）

**问题**：方法前半段收集的 `checks`/`warnings` 在 `assess_trade()` 返回 assessment dict 后被丢弃。

**修复**：在 assessment 中合并前序收集的数据。

### Fix-4: TradeOutcomeTracker close_price=None 静默丢弃（已修复 ✅ — 前序会话）

**问题**：`on_position_closed()` 在 `close_price=None` 时直接 return，交易无终态记录。

**修复**：记录为 unresolved 终态（`won=None`），仍写入 DB 供审计，暴露 `unresolved_closes` 计数。

---

## 三、已知未修复的问题

### Issue-1: 置信度公式中的缩放系数仍为硬编码

**现状**：每个策略的 `evaluate()` 方法中存在大量"指标值 → confidence"的映射公式，包含硬编码的缩放系数和加成值。

**示例**：
```python
# SmaTrendStrategy: 价差 → confidence
confidence = min(abs(spread) * 100, 1.0)  # 系数 100

# EmaRibbonStrategy: EMA 三线间距 → confidence
confidence = 0.5 + total_span * 80  # 基础 0.5, 系数 80

# BollingerBreakoutStrategy: squeeze bonus
squeeze_bonus = 0.3 - band_width * 10  # 基础 0.3, 系数 10

# 所有策略：市场结构加成
confidence += 0.22  # sweep_level 加成
confidence -= 0.20  # rejected_breakout 扣分
```

**影响**：约 70+ 个魔法数字散布在 5 个策略文件中。修改需要改代码。

**建议**：**暂不配置化**。原因：
1. 这些系数高度策略特定，配置到 INI 会导致配置文件膨胀（每策略 10+ 参数）
2. 使用频率低——这些是策略内部逻辑，不像阈值那样频繁调优
3. 可考虑未来引入 per-strategy JSON 配置文件（如 `config/strategies/trend.json`），按需加载

**状态**：Backlog — 待架构稳定后按需推进

### Issue-2: Hot Reload 不覆盖 regime_detector 和 strategy_params

**现状**：`register_signal_hot_reload()` 中更新了 `signal_runtime.update_policy()` / `trade_executor.config` / `performance_tracker._config` 等，但**没有**：
- 更新 `signal_runtime._regime_detector` 的阈值
- 重新调用 `_apply_strategy_config_overrides()` 应用新的策略参数

**影响**：修改 `[regime_detector]` 或 `[strategy_params]` 后需手动重启服务。

**建议**：P1 优先级。实现 `MarketRegimeDetector.update_thresholds()` 和重新应用 strategy_params 的逻辑。

**状态**：Backlog — 不影响生产运行（重启即可），待其他 P0 项完成后处理

### Issue-3: TimeframeScaler 缩放因子硬编码

**现状**：`src/signals/strategies/base.py` 中 `SCALE_FACTORS = {"M1": 0.60, "M5": 0.75, ...}` 硬编码在类属性中。

**影响**：无法通过配置调整不同时间框架的阈值缩放比例。

**建议**：低优先级。当前值是经过验证的经验值，短期内无需调整。可在需要时配置化。

**状态**：Backlog

### Issue-4: 市场结构加成值离散度大，无统一规范

**现状**：各策略中市场结构加成值从 +0.08 到 +0.22、扣分从 -0.15 到 -0.22，没有统一的标准。

**影响**：调优时需要逐策略查找，难以系统性调整。

**建议**：P2 优先级。可考虑定义一组标准加成等级（如 small=0.08, medium=0.12, large=0.18），策略只引用等级而非具体数值，等级值可配置化。

**状态**：Backlog

---

## 四、配置化参数全景表

### 全局影响参数（修改后影响所有策略）

| Section | 参数 | 当前值 | 范围 | Impact |
|---------|------|--------|------|--------|
| `[regime_detector]` | adx_trending_threshold | 23.0 | [18, 30] | ADX ≥ 此值 → TRENDING。调低 → 更多趋势判定 → 趋势策略更活跃 |
| `[regime_detector]` | adx_ranging_threshold | 18.0 | [12, 22] | ADX < 此值 → RANGING。调高 → 更多震荡判定 → 均值回归策略更活跃 |
| `[regime_detector]` | bb_tight_pct | 0.008 | [0.003, 0.015] | BB width < 此值 + ADX < ranging → BREAKOUT(蓄力)。调高 → 更宽松的蓄力判定 |
| `[regime]` | min_affinity_skip | 0.15 | [0.0, 0.30] | affinity < 此值的策略直接跳过。调低 → 更多策略参与（CPU 开销增加） |
| `[regime]` | soft_regime_enabled | true | bool | 概率化 Regime 分类。true → 消除硬分类阈值边界跳变 |
| `[voting]` | consensus_threshold | 0.40 | [0.30, 0.60] | 买/卖方需占总权重的最低比例。调高 → 更难达成共识 → 信号更少但更可靠 |
| `[voting]` | min_quorum | 2 | [1, 5] | 最少非hold策略数。调高 → 要求更多策略参与 |
| `[voting]` | disagreement_penalty | 0.50 | [0.0, 1.0] | 买卖分歧惩罚。调高 → 分歧时置信度降低更多 |

### 策略级参数（修改后仅影响指定策略）

| 策略 | 参数 | 当前值 | 范围 | Impact |
|------|------|--------|------|--------|
| supertrend | adx_threshold | 23.0 | [18, 30] | ADX < 此值 → hold。调低 → 更多信号 |
| roc_momentum | adx_min | 23.0 | [18, 30] | 同上 |
| roc_momentum | roc_threshold | 0.10 | [0.05, 0.30] | ROC 绝对值门槛。调低 → 更敏感 |
| donchian_breakout | adx_min | 23.0 | [18, 30] | 同上 |
| rsi_reversion | overbought | 70 | [65, 80] | RSI 超买阈值。调高 → 更少信号但更极端 |
| rsi_reversion | oversold | 30 | [20, 35] | RSI 超卖阈值。调低 → 同上 |
| williams_r | overbought | -20 | [-10, -25] | %R 超买。调高（更接近 0）→ 更少信号 |
| williams_r | oversold | -80 | [-75, -90] | %R 超卖。调低（更接近 -100）→ 更少信号 |
| cci_reversion | upper_threshold | 100 | [80, 150] | CCI 超买。调高 → 需更极端才触发 |
| cci_reversion | lower_threshold | -100 | [-80, -150] | CCI 超卖。调低 → 同上 |
| stoch_rsi | overbought | 80 | [75, 90] | K线超买。调高 → 更极端才触发 |
| stoch_rsi | oversold | 20 | [10, 25] | K线超卖。调低 → 同上 |
| session_momentum | london_min_atr_pct | 0.00045 | [0.0003, 0.0006] | 伦敦盘最低 ATR %。调高 → 过滤更多低波动信号 |
| session_momentum | other_min_atr_pct | 0.00035 | [0.0002, 0.0005] | 其他时段最低 ATR % |

### 执行层参数

| Section | 参数 | 当前值 | Impact |
|---------|------|--------|--------|
| `[signal]` | auto_trade_min_confidence | 0.70 | 自动下单最低置信度门槛 |
| `[signal]` | auto_trade_require_armed | true | 是否要求 armed 状态 |
| `[signal]` | max_concurrent_positions_per_symbol | 3 | 单品种最大并发仓位 |
| `[circuit_breaker]` | max_consecutive_failures | 3 | 连续失败熔断阈值 |
| `[htf_cache]` | max_age_seconds | 14400 | HTF 方向缓存过期时间（秒） |

### Regime 亲和度

通过 `[regime_affinity.<strategy>]` section 覆盖。全部 20 个策略均已配置。

调优指南：
- **趋势策略**：trending 高（0.85-1.0），ranging 低（0.10-0.20）
- **均值回归策略**：ranging 高（0.95-1.0），trending 低（0.20-0.30）
- **突破策略**：breakout 高（1.0），其他视具体策略而定
- **uncertain** 通常设为 0.45-0.65（中性偏保守）

---

## 五、硬编码保留清单（不应配置化的参数）

以下参数因属于算法常量或市场约定，保持硬编码：

| 类别 | 参数 | 值 | 保留理由 |
|------|------|---|---------|
| Soft Regime 内部 | `detect_soft()` 评分权重 | 0.05-0.90 | 概率分类算法内部常数 |
| Hold 基础 confidence | 各策略 hold 状态 | 0.10-0.20 | "无方向"的设计常数 |
| RSI confidence 公式 | `(oversold - rsi) / 30 + 0.4` | 分母 30, 偏移 0.4 | 数学归一化常数 |
| WR confidence 公式 | `depth / 20.0` | 分母 20.0 | %R 区间归一化（-80 到 -100 = 20 点范围） |
| 策略类属性 | name, category, required_indicators, preferred_scopes | 各不相同 | 策略身份标识，不可运行时变更 |
| TimeframeScaler | M1=0.60 ... D1=1.30 | 缩放表 | 经验值，变更频率极低 |
| 信号状态机 | preview → armed → confirmed 转换逻辑 | 状态转换规则 | 核心算法，不应暴露 |

---

## 六、推荐后续步骤

### P0（当前迭代已完成）
- [x] Regime 检测阈值配置化
- [x] 置信度流水线全局参数配置化（min_affinity_skip, confidence_floor）
- [x] 策略级可调参数配置化（阈值、ADX 门槛）
- [x] Regime 亲和度外部化
- [x] 修复 5 个策略 `__init__` 缺失导致配置无效
- [x] Broker-Aware Precheck（trade_mode / stops_level / freeze_level）
- [x] Close-Result Completeness（unresolved 终态记录）

### P1（下一迭代）
- [ ] Hot Reload 覆盖 regime_detector 和 strategy_params
- [ ] 为均值回归策略的 delta bonus 参数配置化（`delta_bonus_max`, `exhaust_threshold`）
- [ ] Calibrator dump/load 在启动时自动加载（减少冷启动校准延迟）
- [ ] Recovery and control-path audit completeness
- [ ] Execution quality reporting

### P2（架构稳定后）
- [ ] 置信度公式中的缩放系数抽取（per-strategy JSON 或 `[strategy_params]` 扩展）
- [ ] 市场结构加成值标准化为等级制
- [ ] TimeframeScaler 缩放因子配置化
- [ ] 支持 per-symbol 策略参数覆盖（`[strategy_params.XAUUSD]`）
- [ ] Operator controls（pause by symbol, disable trailing, close-only mode）
- [ ] Partial close and scale-out management

### Backlog（长期）
- [ ] ML 模型替换 `_get_calibration_factor()`（校准器子类化预留已就绪）
- [ ] 参数遗传算法自动调优框架（需要历史回放基础设施）
- [ ] 策略配置版本化 + A/B 测试框架

---

## 七、配置调优入口指南

当需要调整信号系统行为时，按以下优先级查找配置入口：

```
1. 想调整"什么行情下哪些策略更活跃" → [regime_affinity.*]
2. 想调整"什么被判定为趋势/震荡/突破" → [regime_detector]
3. 想调整"某个策略的灵敏度" → [strategy_params]
4. 想调整"多策略共识的门槛" → [voting] / [voting_group.*]
5. 想调整"信号到下单的过滤严格度" → [signal] auto_trade_* / [circuit_breaker]
6. 想调整"策略在哪些时段/时间框架运行" → [strategy_sessions] / [strategy_timeframes]
7. 想调整"日内绩效反馈强度" → [performance_tracker]
8. 想调整"长期校准的介入力度" → calibrator alpha/min_samples (在代码中配置)
```

所有配置修改后重启服务即可生效。Hot Reload 支持的参数（voting、execution、performance_tracker）可通过 API 热加载。
