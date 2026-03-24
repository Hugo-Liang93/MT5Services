# 实时/回测统一内核设计

> 目标：让实盘和回测共用同一套"策略评估 + 指标计算 + 置信度管线"核心逻辑，
> 消除双轨结构，使策略变更只需改一处代码。

---

## 1. 当前共享状态

回测系统已经做得很好 — 大部分核心逻辑已直接复用实盘代码：

### 1.1 已共享（零重复）

| 能力 | 实盘组件 | 回测使用方式 |
|------|---------|------------|
| 指标计算 | `OptimizedPipeline.compute()` | 独立实例，同一份 `indicators/core/` 函数 |
| 策略评估 | `SignalModule.evaluate()` | 独立实例，含完整置信度管线 |
| 过滤器 | `SignalFilterChain.should_evaluate()` | 直接复用，注入 `bar.time` |
| Regime 检测 | `MarketRegimeDetector.detect()` | 独立实例 |
| SL/TP 计算 | `sizing.compute_trade_params()` | 纯函数直接导入 |
| 持仓规则 | `position_rules.*` | 纯函数直接导入 |
| 价格区间 | `pending_entry.compute_entry_zone()` | 纯函数直接导入 |
| 策略参数覆盖 | `_apply_strategy_config_overrides()` | 同一函数 |
| Regime 亲和度覆盖 | 配置驱动 | 同一格式 |
| 参数推荐 | `ConfigApplicator` → `signal.local.ini` + 热更新 | `RecommendationEngine` 从 WF 结果生成 |

> **参数推荐闭环**（新增）：Walk-Forward 验证 → `RecommendationEngine.generate()` → 人工审核 → `ConfigApplicator.apply()` → signal.local.ini 原子写入 + `SignalModule.apply_param_overrides()` 热更新。回滚时从备份恢复。推荐基于 OOS regime-specific 胜率生成亲和度建议，基于多窗口 best_params 中位数生成策略参数建议。

### 1.2 已独立实现（有理由的差异）

| 能力 | 实盘 | 回测 | 差异原因 |
|------|------|------|---------|
| 数据加载 | MT5 API → MarketDataService | DB → HistoricalDataLoader | 数据源不同 |
| SL/TP 触发 | MT5 服务器自动触发 | PortfolioTracker 逐 bar 检查 | 执行环境不同 |
| 资金曲线 | 账户余额实时查询 | equity_curve 内存追踪 | 执行环境不同 |
| 信号评估记录 | SignalQualityTracker | engine._record_evaluation() | 用途不同 |

### 1.3 潜在双轨风险（需关注的点）

| 逻辑 | 实盘位置 | 回测位置 | 风险 |
|------|---------|---------|------|
| 置信度管线 | `SignalRuntime._evaluate_strategies()` 内联 | `BacktestEngine._evaluate_strategies()` 内联 | 两处独立实现，修改需同步 |
| HTF 对齐乘数 | `SignalRuntime._compute_htf_alignment()` | `BacktestEngine._apply_htf_alignment()` | 两处独立实现 |
| Intrabar 衰减 | `SignalRuntime` 内联 | `BacktestEngine` 内联 | 参数可能不一致 |
| 状态机 | `SignalRuntime._transition_*()` | `BacktestEngine._BacktestSignalState` | 回测简化版 |
| Voting | `StrategyVotingEngine` + runtime 协调 | engine 内联调用 VotingEngine | 流程差异 |

---

## 2. 统一方向

### 2.1 核心思路

将**可复用的纯逻辑**从 runtime 中提取为独立函数/类，实盘和回测都调用同一份：

```
提取前：
  SignalRuntime._evaluate_strategies()     ← 1,200+ 行，内联全部逻辑
  BacktestEngine._evaluate_strategies()    ← ~200 行，部分逻辑复制

提取后：
  signal_evaluator.evaluate_snapshot()     ← 纯函数/类，无线程/队列依赖
  SignalRuntime → 调用 signal_evaluator    ← 事件驱动适配
  BacktestEngine → 调用 signal_evaluator   ← 逐 bar 适配
```

### 2.2 可提取的纯逻辑

| 逻辑 | 当前位置 | 提取目标 | 签名 |
|------|---------|---------|------|
| 置信度修正 | 两处内联 | `confidence.apply_pipeline()` | `(raw, affinity, perf_mul, calibrator, floor, decay, htf_mul) → final` |
| HTF 对齐计算 | 两处内联 | `htf.compute_alignment_multiplier()` | `(htf_context, scope) → float` |
| Intrabar 衰减 | 两处内联 | `confidence.apply_intrabar_decay()` | 已存在，确认两处引用同一函数 |
| Affinity gate | 两处内联 | `affinity.check_eligible()` | `(strategy, regime, min_skip) → bool` |
| 快照去重 | runtime 内联 | 保持 runtime 内部 | 仅实盘需要 |

### 2.3 不应统一的逻辑

| 逻辑 | 原因 |
|------|------|
| 队列管理 | 实盘特有（回测无队列） |
| 线程管理 | 实盘特有 |
| 状态持久化 | 实盘写 DB，回测写内存 |
| 信号广播 | 实盘有 listener chain，回测直接处理 |

---

## 3. 分阶段实施计划

### Phase A：提取置信度管线函数（最高优先级）

这是双轨风险最大的部分。

**目标**：创建 `src/signals/evaluation/confidence_pipeline.py`

```python
def apply_confidence_pipeline(
    raw_confidence: float,
    effective_affinity: float,
    performance_multiplier: float,
    calibrator: Optional[ConfidenceCalibrator],
    confidence_floor: float,
    intrabar_decay: Optional[float],    # scope=intrabar 时传入
    htf_multiplier: float,              # 1.0 = 无 HTF 数据
    strategy: str,
    action: str,
    regime: Optional[RegimeType],
) -> float:
    """统一的置信度修正管线。实盘和回测共用。"""
    conf = raw_confidence * effective_affinity
    conf *= performance_multiplier
    if calibrator:
        conf = calibrator.calibrate(strategy, action, regime, conf)
    conf = max(conf, confidence_floor)
    if intrabar_decay is not None:
        conf *= intrabar_decay
    conf *= htf_multiplier
    return conf
```

**迁移步骤**：
1. 创建函数
2. `SignalRuntime._evaluate_strategies()` 调用该函数替换内联逻辑
3. `BacktestEngine._evaluate_strategies()` 调用该函数替换内联逻辑
4. 添加单元测试验证两处输出一致

---

### Phase B：提取 HTF 对齐计算函数

**目标**：确保 `compute_htf_alignment_multiplier()` 实盘和回测使用同一函数。

当前状态：
- 实盘 `SignalRuntime._compute_htf_alignment()` — 完整实现
- 回测 `BacktestEngine._apply_htf_alignment()` — 简化版

**迁移步骤**：
1. 将实盘逻辑提取为 `src/signals/evaluation/htf_alignment.py` 的纯函数
2. 回测调用同一函数
3. 回测的 HTF 预加载结果转换为与实盘相同的 HTFDirectionContext 格式

---

### Phase C：统一 Regime + Affinity 计算

**目标**：确保 Regime 检测和 affinity 计算共用同一逻辑。

当前状态：
- 回测已使用独立 `MarketRegimeDetector` 实例 — 逻辑相同
- affinity 计算在两处内联 — 可提取

**迁移步骤**：
1. 提取 `compute_effective_affinity(strategy_affinity, regime_result, soft_enabled)` 为纯函数
2. 两处引用同一函数

---

### Phase D：回测状态机对齐（低优先级）

当前回测使用简化版状态机（`_BacktestSignalState`），与实盘的 `RuntimeSignalState` 有差异。

**评估**：对于逐 bar 回测，简化版状态机是合理的（无 preview/armed 路径）。
仅当需要"回测 intrabar 信号"时才需要对齐。

**建议**：暂不对齐，等 Phase 4（SignalRuntime 拆分）后自然收敛。

---

## 4. 验收标准

### 功能等价性

- [ ] 对同一组历史 bar，实盘 evaluate + 回测 evaluate 产生相同的置信度值
- [ ] HTF 对齐乘数在相同输入下输出一致
- [ ] Regime 分类在相同指标下输出一致

### 代码质量

- [ ] 置信度管线函数有完整单元测试
- [ ] HTF 对齐函数有完整单元测试
- [ ] 无重复的置信度计算逻辑（grep 验证）

### 回归安全

- [ ] 所有现有测试通过
- [ ] 回测结果与重构前一致（对比同一参数集的 metrics）

---

## 5. 长期目标（Phase 4 之后）

当 SignalRuntime 完成拆分（evaluator / state_machine 分离）后：

```
src/signals/
├── evaluator/
│   ├── confidence_pipeline.py    ← 统一置信度管线
│   ├── htf_alignment.py          ← 统一 HTF 对齐
│   ├── affinity.py               ← 统一 affinity 计算
│   └── strategy_evaluator.py     ← 策略评估编排（无队列/线程）
│
├── state_machine/                ← 实盘状态机
│
└── orchestration/
    └── runtime.py                ← 事件驱动适配层（队列/线程/广播）

src/backtesting/
└── engine.py                     ← 逐 bar 适配层
    → 直接调用 evaluator/ 中的函数
```

**最终目标**：`evaluator/` 是实盘和回测共享的"决策内核"，不含任何运行时绑定。
