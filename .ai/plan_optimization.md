# MT5Services 持续迭代优化计划

> 日期: 2026-03-22 | 两轮共 6 小时迭代
> 目标: 多指标融合 + 多 TF 决策 + 模块化清晰 + 压力测试稳定 + 满足 XAUUSD 日内交易

---

## 评分总览

| 维度 | 初始 | R1 | R2 | R3 | R4 | R5 | R6 | R6.5 | R7 | R8 |
|------|------|-----|-----|-----|-----|-----|-----|------|-----|-----|
| 策略信号质量 | 6.0 | 7.5 | 8.2 | 8.2 | 8.5 | 8.8 | 9.0 | 9.0 | 9.0 | **9.2** |
| 指标架构健壮性 | 7.2 | 8.2 | 8.5 | 8.5 | 9.0 | 9.0 | 9.2 | 9.3 | 9.5 | **9.5** |
| 多 TF 融合能力 | 2.0 | 7.5 | 7.5 | 7.5 | 7.5 | 7.5 | 8.5 | 8.5 | 8.5 | **8.7** |
| 交易执行完整性 | 7.5 | 7.5 | 8.5 | 9.0 | 9.0 | 9.2 | 9.2 | 9.5 | 9.5 | **9.5** |
| 风控覆盖度 | 6.5 | 6.5 | 8.0 | 9.0 | 9.0 | 9.2 | 9.2 | 9.2 | 9.2 | **9.3** |
| 测试覆盖 | 5.0 | 6.0 | 6.0 | 6.5 | 7.0 | 7.2 | 7.2 | 7.2 | 8.5 | **8.8** |

---

## Round 1 完成清单（3 小时）

### 基础设施安全加固
- [x] NaN/Inf 防护 — Pipeline 层 `sanitize_result()` 统一拦截
- [x] Preview Snapshot 线程安全 — `_results_lock` 保护 OrderedDict
- [x] Intrabar 置信度衰减 — 默认 ×0.85，可配 `intrabar_confidence_decay`

### 策略质量增强
- [x] BollingerBreakout BB 宽度过滤 — 挤压中 ×0.70
- [x] MACD 动量加速度 — hist_d3 加分 + 弱动量降权
- [x] MultiTimeframeConfirm — 启用 htf_indicators 读取 H1 ema50/sma20

### 配置调优
- [x] min_affinity_skip 0.15 → 0.25
- [x] RSI overbought 70 → 75
- [x] CCI upper_threshold 100 → 150
- [x] 12 个压力测试（NaN、并发、HTF 吞吐量）

### HTF 指标架构（Round 1 前完成）
- [x] SignalContext.htf_indicators 字段
- [x] SignalRuntime 自动解析注入
- [x] 启动时 HTF 声明校验
- [x] 17 个 HTF 单元测试

---

## Round 2 完成清单（3 小时）

### 风控集成
- [x] **DailyLossLimitRule 集成到 TradeExecutor** — `daily_loss_check_fn` 回调注入，超日损 5% 阻止新开仓
- [x] **波动率异常过滤器** — ATR 超 baseline 2.5 倍时跳过整个快照的策略评估

### Regime 检测增强
- [x] **RSI 动量辅助** — ADX 23-28 区间内 RSI 40-60 时降级为 UNCERTAIN（过滤假趋势）

### 策略精细化（5 个策略改进）
- [x] **KeltnerBBSqueeze 紧度量化** — squeeze_tightness 分段加权（极紧+0.30 / 中度+0.20 / 轻度+0.10）
- [x] **SqueezeReleaseFollow MACD 加速度** — hist_d3 同向加速 +0.10，衰减 -0.08
- [x] **RocMomentum ATR 归一化** — 动态阈值 `max(atr_pct × 0.2, 基准)`，新增 atr14 依赖
- [x] **SmaTrend 方向稳定性过滤** — 均线差距 < 0.05% 时 hold，防止震荡市反复交叉

### 配置新增
- [x] `volatility_atr_spike_multiplier = 2.5` — 波动率异常阈值
- [x] `daily_loss_limit_pct = 5.0` — 日损失限制百分比

---

## 完整改动文件汇总（两轮共计）

### 核心架构层
| 文件 | 改动 |
|------|------|
| `src/signals/models.py` | SignalContext 新增 htf_indicators |
| `src/signals/service.py` | evaluate() htf 参数 + strategy_htf_indicators() + 校验 |
| `src/signals/orchestration/runtime.py` | HTF 注入 + Intrabar 衰减 + 波动率过滤 + 缓存构建 |
| `src/indicators/core/base.py` | sanitize_result() |
| `src/indicators/engine/pipeline.py` | Pipeline NaN/Inf 拦截 |
| `src/indicators/snapshot_publisher.py` | Preview Snapshot 加锁 |
| `src/indicators/manager.py` | _store_preview_snapshot/get_intrabar_snapshot 持锁 |

### 策略层（8 个策略增强）
| 文件 | 改动 |
|------|------|
| `src/signals/strategies/breakout.py` | BollingerBreakout BB 宽度过滤 + MTF htf_indicators + KeltnerBBSqueeze 紧度 + SqueezeRelease MACD 加速度 |
| `src/signals/strategies/trend.py` | MACD 动量加速度 + RocMomentum ATR 归一化 + SmaTrend 方向稳定性 |
| `src/signals/evaluation/regime.py` | detect() RSI 动量辅助 + _extract_rsi() |

### 交易执行层
| 文件 | 改动 |
|------|------|
| `src/trading/signal_executor.py` | daily_loss_check_fn 回调 + 日损失检查逻辑 |
| `src/api/factories/signals.py` | HTF 校验 + 日损失回调 + 波动率配置传递 |

### 配置层
| 文件 | 改动 |
|------|------|
| `src/config/models/signal.py` | htf_indicators_enabled + intrabar_confidence_decay + daily_loss_limit_pct + volatility_atr_spike_multiplier |
| `src/config/signal.py` | [htf_indicators] 解析 |
| `config/signal.ini` | [htf_indicators] + 参数调优 + 波动率 + 日损限制 |

### 测试
| 文件 | 改动 |
|------|------|
| `tests/signals/test_htf_indicators.py` | 17 个 HTF 测试 |
| `tests/signals/test_signal_runtime.py` | DummySignalService 兼容修复 |
| `tests/stress/test_indicator_robustness.py` | 12 个压力测试 |

### 测试结果: **490 passed, 0 failed**

---

## Round 3 完成清单

### 风控新增规则（3 项）
- [x] **MarginAvailabilityRule** — 保证金动态检查：execute_trade 前估算保证金，与 free_margin × safety_factor 对比
  - `margin_safety_factor = 1.2`（risk.ini 可配）
  - 估算保证金注入 `intent.metadata["estimated_margin"]`
  - 不足时 block，account 不可用时 warn
- [x] **TradeFrequencyRule** — 交易频率限制器：有状态规则，维护执行时间戳
  - `max_trades_per_day = 20`、`max_trades_per_hour = 5`（risk.ini 可配）
  - 成功执行后 `PreTradeRiskService.record_trade_execution()` 回调
  - 48h 自动裁剪旧条目
- [x] **时间框架差异化风险百分比** — sizing.py 新增 `TIMEFRAME_RISK_MULTIPLIER`
  - M1: 0.50, M5: 0.75, M15: 1.00, H1: 1.20
  - `compute_trade_params()` 自动应用 `effective_risk_pct = base × multiplier`

### 接线变更
- [x] `TradingService.execute_trade()` — 先估算保证金再调 risk service（顺序调整）
- [x] `TradingService.execute_trade()` — 成功后调 `record_trade_execution()`
- [x] `PreTradeRiskService` — 注册 MarginAvailabilityRule + TradeFrequencyRule
- [x] `RiskConfig` — 新增 margin_safety_factor / max_trades_per_day / max_trades_per_hour
- [x] `centralized.py` — 新增可选字段 empty→None 处理

### 测试新增
| 文件 | 测试数 |
|------|--------|
| `tests/risk/test_risk_rules.py` | 20 个（MarginAvailabilityRule 10 + TradeFrequencyRule 10）|
| `tests/trading/test_sizing.py` | 12 个（risk multiplier 7 + compute_trade_params 5）|

### 测试结果: **531 passed, 0 failed**

---

## 改动文件汇总（Round 3）

### 风控规则层
| 文件 | 改动 |
|------|------|
| `src/risk/rules.py` | 新增 MarginAvailabilityRule + TradeFrequencyRule |
| `src/risk/service.py` | 注册两个新规则 + record_trade_execution() 方法 |

### 交易执行层
| 文件 | 改动 |
|------|------|
| `src/trading/sizing.py` | TIMEFRAME_RISK_MULTIPLIER + resolve_timeframe_risk_multiplier() |
| `src/trading/trading_service.py` | 保证金估算前置 + 频率记录回调 |

### 配置层
| 文件 | 改动 |
|------|------|
| `src/config/models/runtime.py` | RiskConfig 新增 3 个字段 |
| `src/config/centralized.py` | 新增可选字段 empty→None |
| `config/risk.ini` | 新增 margin_safety_factor / max_trades_per_day / max_trades_per_hour |

### 测试
| 文件 | 改动 |
|------|------|
| `tests/risk/__init__.py` | 新建 |
| `tests/risk/test_risk_rules.py` | 20 个新测试 |
| `tests/trading/test_sizing.py` | 12 个新测试 |

---

## Round 4 完成清单

### 指标正确性修复
- [x] **StochRSI Wilder 平滑修复** — RSI 序列改用 Wilder 指数平滑（与 `rsi()` 函数一致）
  - 原实现每个窗口用简单平均计算 RSI（与 MT5/TradingView 不一致）
  - 新增 `_rsi_series_wilder()` 辅助函数，一次遍历构建完整 RSI 序列
  - 性能同步改善：从 O(N × period) 重复计算降至 O(N) 单次遍历

### 指标算法优化
- [x] **HMA 算法精简** — 只计算最后 `sqrt_period` 个 hull 差值
  - 原实现从 `period-1` 遍历到末尾（多余迭代）
  - 优化后仅生成 WMA 最终需要的 `sqrt_period` 个点

### 并发安全测试套件（9 个新测试）
- [x] `TestTradeFrequencyRuleConcurrency`（2 个）：10 写线程 + 5 读线程并发
- [x] `TestMarketDataServiceConcurrency`（4 个）：quote/tick/OHLC/mixed 多线程压力
- [x] `TestIndicatorComputationConcurrency`（3 个）：HMA/StochRSI/RSI 8 线程并发计算

### 改动文件汇总（Round 4）

| 文件 | 改动 |
|------|------|
| `src/indicators/core/momentum.py` | `_rsi_series_wilder()` + `stoch_rsi()` 重写用 Wilder 平滑 |
| `src/indicators/core/mean.py` | `hma()` 优化：只算最后 sqrt_period 个 hull 值 |
| `tests/stress/test_concurrency.py` | 9 个新并发压力测试 |

### 测试结果: **540 passed, 0 failed**

---

## Round 5 完成清单

### 正确性修复
- [x] **profit_factor 数值稳定性** — 上限钳位到 50.0，防止 total_loss_pnl 接近零时产生极端值
- [x] **纯利策略乘数遗漏** — 当策略全赢无亏（pf=None 且 wins>0）时给予 ×1.05 微幅提升

### 性能优化
- [x] **PerformanceTracker.describe() O(N²) → O(N)** — 提取 `_regime_reverse_index()` 反向索引，`strategy_ranking()` 同步优化

### 一致性修复
- [x] **CompositeStrategy 分歧因子** — 从线性衰减改为平方衰减（与 VotingEngine 统一）

### 配置化增强
- [x] **TIMEFRAME_RISK_MULTIPLIER 配置化** — 新增 `signal.ini [timeframe_risk]` section
  - INI 配置优先于 sizing.py 内置默认值
  - `compute_trade_params()` 新增 `timeframe_risk_overrides` 参数
  - 通过 ExecutorConfig → build_executor_config → signal.ini 完整链路

### 改动文件汇总（Round 5）

| 文件 | 改动 |
|------|------|
| `src/signals/evaluation/performance.py` | profit_factor clamp + 纯利乘数 + `_regime_reverse_index()` |
| `src/signals/strategies/composite.py` | 分歧因子改为平方衰减 |
| `src/trading/sizing.py` | `resolve_timeframe_risk_multiplier()` 支持 overrides 参数 |
| `src/trading/signal_executor.py` | ExecutorConfig 新增 timeframe_risk_multipliers |
| `src/config/models/signal.py` | SignalConfig 新增 timeframe_risk_multipliers |
| `src/config/signal.py` | 解析 [timeframe_risk] section |
| `src/api/factories/signals.py` | 传递 timeframe_risk_multipliers |
| `config/signal.ini` | 新增 [timeframe_risk] section |
| `tests/trading/test_sizing.py` | 新增 4 个 override 测试 |
| `tests/signals/evaluation/test_performance.py` | 新增 3 个测试（PF clamp/纯利/纯亏）|

### 测试结果: **547 passed, 0 failed**

---

## Round 6 完成清单（多 TF 融合增强）

### HTF 方向强度化
- [x] **HTFStateCache 增强** — 从缓存 `(direction, updated_at)` 升级为 `HTFDirectionContext(direction, confidence, regime, stable_bars, updated_at)`
  - 信号监听器自动提取 event.confidence 和 _regime 元数据
  - stable_bars 追踪同方向连续 bar 数

### HTF 对齐修正多维度化
- [x] **`_compute_htf_alignment()` 新方法** — 替代简单二元 ×1.10/×0.70
  - 强度加权：HTF 高置信度信号放大效果（`1.0 + (confidence - 0.5) * 0.3`）
  - 稳定性加权：HTF 方向持续多 bar 放大效果（`min(1.0 + (stable_bars - 1) * 0.03, 1.15)`）
  - Intrabar 半强度：未收盘信号的 HTF 修正幅度减半（`1.0 + (mul - 1.0) * 0.5`）
  - 向下兼容：无 htf_context_fn 时回退到 htf_direction_fn

### 策略 HTF 指标补全（5 个策略）
- [x] **SupertrendStrategy** — `htf_indicators = {"H1": ("supertrend14", "adx14")}`
  - H1 supertrend 同向 → +0.08，反向 → -0.05
- [x] **DonchianBreakoutStrategy** — `htf_indicators = {"H1": ("donchian20", "adx14")}`
  - close 突破 H1 通道 → +0.10，反向 → -0.05
- [x] **BollingerBreakoutStrategy** — `htf_indicators = {"H1": ("boll20",)}`
  - H1 BB 也挤压时 → +0.06
- [x] **SmaTrendStrategy** — `htf_indicators = {"D1": ("sma20", "ema50")}`
  - D1 均线方向一致 → +0.10，反向 → -0.08
- [x] **MacdMomentumStrategy** — `htf_indicators = {"D1": ("macd",)}`
  - D1 MACD hist 同向 → +0.08，反向 → -0.06

### D1 时间框架启用
- [x] `config/app.ini` timeframes 新增 D1 — H1 策略获得日线级确认

### 代码质量修复
- [x] **runtime.py `import dataclasses` 热路径重复导入** — 移到文件顶部（3 处内联导入清除）

### 改动文件汇总（Round 6）

| 文件 | 改动 |
|------|------|
| `src/signals/strategies/htf_cache.py` | HTFDirectionContext dataclass + get_htf_context() + on_signal_event 增强 |
| `src/signals/orchestration/runtime.py` | `_compute_htf_alignment()` + htf_context_fn + import 清理 |
| `src/signals/strategies/trend.py` | supertrend/sma_trend/macd_momentum HTF 声明 + evaluate 增强 |
| `src/signals/strategies/breakout.py` | donchian/bollinger HTF 声明 + evaluate 增强 |
| `src/api/factories/signals.py` | 传递 htf_context_fn |
| `config/app.ini` | timeframes 新增 D1 |
| `tests/config/test_config_centralization.py` | 更新 timeframes 断言 |

### 测试结果: **547 passed, 0 failed**

---

## Round 6.5 完成清单（模块间调度审查修复）

### 高严重度修复
- [x] **TradeExecutor 异步化** — `on_signal_event()` 改为非阻塞入队（内部 64 容量队列 + daemon 工作线程）
  - 防止 MT5 API 慢调用（100ms~5s+）阻塞 SignalRuntime 主循环
  - 新增 `flush()` 方法供测试同步等待
  - 新增 `shutdown()` 方法供 lifespan 优雅关闭
  - lifespan.py 注册 TradeExecutor shutdown

- [x] **SignalRuntime 队列扩容** — confirmed 2048→4096, intrabar 4096→8192
  - 降低快照发布背压阻塞 IndicatorManager 的概率
  - backpressure timeout 保持 0.2s 作为最后手段

### 微优化
- [x] **Soft Regime 解析缓存** — 同一快照内 N 个策略共享一次 `SoftRegimeResult.from_dict()` 解析
  - 之前每个策略重复解析一次（20 策略 × from_dict = 20 次哈希查找）
  - 现在预解析一次，通过 `_parsed_cache` 参数传递

### 改动文件汇总（Round 6.5）

| 文件 | 改动 |
|------|------|
| `src/trading/signal_executor.py` | 异步队列 + 工作线程 + flush/shutdown |
| `src/signals/orchestration/runtime.py` | 队列扩容 + _soft_parsed 预解析 + _effective_affinity 缓存参数 |
| `src/api/lifespan.py` | 注册 trade_executor.shutdown |
| `tests/trading/test_signal_executor.py` | _fire() helper 适配异步化 |
| `tests/signals/test_signal_runtime.py` | 队列容量断言更新 |

### 测试结果: **547 passed, 0 failed**

---

## Round 7 完成清单（测试覆盖增强）

### 新增测试文件

| 文件 | 测试数 | 覆盖范围 |
|------|--------|---------|
| `tests/indicators/test_core_functions.py` | **53** | sma/ema/hma/rsi/macd/stoch_rsi/cci/williams_r/roc 全面覆盖 |
| `tests/signals/test_htf_cache.py` | **22** | HTFDirectionContext 数据结构 + 缓存查询/过期/写入/stable_bars/并发 |
| `tests/signals/test_strategies_htf.py` | **15** | 5 策略 HTF bonus（aligned/conflict/absent × 5） |
| `tests/trading/test_executor_async.py` | **6** | 非阻塞入队/flush/shutdown/队列溢出/异常/懒启动 |
| `tests/signals/test_signal_runtime.py` | **+12** | _compute_htf_alignment 多维度权重（强度/稳定性/intrabar/组合） |

### 修复的集成测试
- [x] `test_circuit_breaker_pauses_and_resets` — 适配 TradeExecutor 异步化（flush + 超时等待）

### 覆盖缺口闭合

| 缺口 | 旧覆盖 | 新覆盖 | 测试数 |
|------|--------|--------|--------|
| 指标核心函数（9 个） | 0% | ~90% | 53 |
| HTFStateCache + HTFDirectionContext | 0% | ~95% | 22 |
| _compute_htf_alignment 多维度 | 5% | ~95% | 12 |
| 5 策略 HTF bonus 逻辑 | 0% | ~85% | 15 |
| TradeExecutor 异步化 | 30% | ~85% | 6 |

### 测试结果: **660 passed, 0 failed**（+113 新测试）

---

## Round 8 完成清单（P2 快速迭代）

### 策略 HTF 补全（2 个新增）
- [x] **KeltnerBollingerSqueezeStrategy** — `htf_indicators = {"H1": ("keltner20", "boll20")}`
  - H1 + LTF 双重挤压 → +0.08
- [x] **RsiReversionStrategy** — `htf_indicators = {"M15": ("rsi14",)}`
  - M15 RSI < 40 同向确认买入 → +0.06；M15 RSI > 60 同向确认卖出 → +0.06

### 架构优化
- [x] **Confirmed/Intrabar 混合轮询** — `_CONFIRMED_BURST_LIMIT = 5`
  - 每连续消费 5 个 confirmed 事件后主动让出给 intrabar 队列
  - 防止 confirmed 突发（多 TF 同时收盘）导致 intrabar 策略饥饿 1-2 秒

### 配置化增强
- [x] **Delta Momentum Bonus 参数配置化** — `configure_delta_params()` + signal.ini
  - d3_scale / d3_cap / d5_threshold / d5_bonus 4 个参数可通过 INI 覆盖
  - 启动时由 `build_signal_components()` 自动调用

### 测试补全
- [x] **波动率/通道指标测试** — bollinger/keltner/donchian/atr/adx 共 25 个新 case

### 改动文件汇总（Round 8）

| 文件 | 改动 |
|------|------|
| `src/signals/strategies/breakout.py` | KeltnerBBSqueeze HTF 声明 + 双重挤压检测 |
| `src/signals/strategies/mean_reversion.py` | RsiReversion HTF 声明 + M15 确认 + configure_delta_params() |
| `src/signals/orchestration/runtime.py` | _CONFIRMED_BURST_LIMIT 混合轮询 |
| `src/config/models/signal.py` | delta_d3_scale/d3_cap/d5_threshold/d5_bonus 字段 |
| `src/api/factories/signals.py` | 启动时调用 configure_delta_params() |
| `config/signal.ini` | delta momentum 参数注释 |
| `tests/indicators/test_core_functions.py` | +25 波动率指标测试 |

### 测试结果: **685 passed, 0 failed**（+25 新测试）

---

## 后续 Follow-up

### P1 — 已完成
| 事项 | 状态 |
|------|------|
| MT5 Broker 合规检查（stops_level / freeze_level） | ✅ 已完成（Round 2 前） |
| 保证金动态检查 | ✅ 已完成（Round 3） |
| 关仓价格重试补救 | ✅ 基础完成（Round 2 前） |
| 交易频率限制器（max_trades_per_day/hour） | ✅ 已完成（Round 3） |
| 时间框架差异化风险百分比 | ✅ 已完成（Round 3） |
| StochRSI Wilder 平滑修复 | ✅ 已完成（Round 4） |
| HMA 算法精简 | ✅ 已完成（Round 4） |
| 并发安全测试套件 | ✅ 基础完成（Round 4，9 个测试） |
| profit_factor 数值稳定性 | ✅ 已完成（Round 5） |
| 纯利策略乘数遗漏 | ✅ 已完成（Round 5） |
| PerformanceTracker O(N²) 优化 | ✅ 已完成（Round 5） |
| CompositeStrategy 分歧因子统一 | ✅ 已完成（Round 5） |
| TIMEFRAME_RISK_MULTIPLIER 配置化 | ✅ 已完成（Round 5） |
| HTF 方向强度化（confidence/regime/stable_bars） | ✅ 已完成（Round 6） |
| Intrabar HTF 半强度对齐修正 | ✅ 已完成（Round 6） |
| 5 策略 HTF 指标补全 | ✅ 已完成（Round 6） |
| D1 时间框架启用 | ✅ 已完成（Round 6） |
| runtime.py 热路径 import 清理 | ✅ 已完成（Round 6） |
| TradeExecutor 异步化（非阻塞监听器） | ✅ 已完成（Round 6.5） |
| SignalRuntime 队列扩容（4096/8192） | ✅ 已完成（Round 6.5） |
| Soft Regime 解析缓存 | ✅ 已完成（Round 6.5） |
| 指标核心函数测试（9 个函数 53 个 case） | ✅ 已完成（Round 7） |
| HTFStateCache 完整单元测试（22 个 case） | ✅ 已完成（Round 7） |
| _compute_htf_alignment 权重测试（12 个 case） | ✅ 已完成（Round 7） |
| 5 策略 HTF bonus 测试（15 个 case） | ✅ 已完成（Round 7） |
| TradeExecutor 异步化测试（6 个 case） | ✅ 已完成（Round 7） |
| KeltnerBBSqueeze + RsiReversion HTF 补全 | ✅ 已完成（Round 8） |
| Confirmed/Intrabar 混合轮询 | ✅ 已完成（Round 8） |
| Delta Momentum Bonus 配置化 | ✅ 已完成（Round 8） |
| 波动率/通道指标测试（25 个 case） | ✅ 已完成（Round 8） |

### P2 — 中期优化
| 事项 | 预估 | 说明 |
|------|------|------|
| 剩余指标函数测试（supertrend/stochastic/volume） | 4h | volatility 已覆盖，剩 momentum 中的 supertrend/stochastic + volume |
| Ichimoku Cloud 指标 | 6h | 新指标 + 策略 + 测试 |
| 关仓高级补救（partial close / limit fallback） | 4h | 当前仅 3 次市价重试 |
| 并发安全测试扩展 | 4h | SignalRuntime / PositionManager / VotingEngine 多线程压力 |
| HMA/StochRSI 增量计算 | 4h | IncrementalIndicator 子类，compute_mode=incremental |
| Voting Group 级 HTF 差异化策略 | 3h | per-group htf_requirement/htf_alignment_min 配置 |
| Ingestor 自适应采集间隔 | 3h | 下游队列利用率 >80% 时自动降速 |

### P3 — 长期
| 事项 | 说明 |
|------|------|
| RSI/Price Divergence 检测 | 价格新高/新低但 RSI 未确认 → 反转信号 |
| EMA Ribbon 多 bar 确认 | 要求连续 N 根 bar 排列一致才触发 |
| BB 宽度历史分位数 | 用 rolling percentile 替代绝对宽度阈值 |
| 策略间信号相关性分析 | 评估策略独立性，优化 voting group 组合 |
| Paper Trading 回测框架 | 历史数据重放，评估策略在不同市况下的表现 |
| 锁粒度细化（MarketDataService per-data-type 锁） | 当前单一 RLock 覆盖全部缓存 |
