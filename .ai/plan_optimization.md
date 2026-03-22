# MT5Services 持续迭代优化计划

> 日期: 2026-03-22 | 两轮共 6 小时迭代
> 目标: 多指标融合 + 多 TF 决策 + 模块化清晰 + 压力测试稳定 + 满足 XAUUSD 日内交易

---

## 评分总览

| 维度 | 初始 | Round 1 后 | Round 2 后 |
|------|------|-----------|-----------|
| 策略信号质量 | 6.0 | 7.5 | **8.2** |
| 指标架构健壮性 | 7.2 | 8.2 | **8.5** |
| 多 TF 融合能力 | 2.0 | 7.5 | 7.5 |
| 交易执行完整性 | 7.5 | 7.5 | **8.5** |
| 风控覆盖度 | 6.5 | 6.5 | **8.0** |
| 测试覆盖 | 5.0 | 6.0 | 6.0 |

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

## 后续 Follow-up

### P1 — 下一轮迭代
| 事项 | 预估 |
|------|------|
| MT5 Broker 合规检查（stops_level / freeze_level） | 4h |
| 保证金动态检查 | 4h |
| 关仓价格重试补救 | 2h |
| 交易频率限制器（max_trades_per_day/hour） | 4h |

### P2 — 中期优化
| 事项 | 预估 |
|------|------|
| StochRSI/HMA O(N^2) → O(N) | 6h |
| 指标函数完整测试（21×6=126 个） | 12h |
| 并发安全测试套件 | 6h |
| 时间框架差异化风险百分比 | 2h |
| Ichimoku Cloud 指标 | 6h |

### P3 — 长期
| 事项 |
|------|
| RSI/Price Divergence 检测 |
| EMA Ribbon 多 bar 确认 |
| BB 宽度历史分位数 |
| 策略间信号相关性分析 |
| Paper Trading 回测框架 |
