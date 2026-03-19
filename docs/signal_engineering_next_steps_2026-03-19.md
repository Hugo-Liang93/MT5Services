# Signal/策略体系下一步工程化优化建议（XAUUSD 日内）

> 目标：把“策略能跑”升级到“策略可观测、可评估、可迭代、可回滚”的工程化交易系统。

## 1. 先做观测层闭环（本周优先）

### 1.1 统一诊断指标与阈值
基于已上线的 `strategy_diagnostics`，建议先定义红线阈值，避免“有数据但没人用”：

- `conflict.conflict_ratio`：
  - `>= 0.35` 视为高冲突（需要策略层清理）
  - `0.20 ~ 0.35` 视为中冲突（需要 regime 分层）
  - `< 0.20` 视为可接受
- `strategy_breakdown[].hold_ratio`：
  - `>= 0.75` 且 `missing_required_count` 高，优先排查指标链路
  - `>= 0.75` 且 `missing_required_count` 低，说明策略逻辑过严或市场不适配
- `strategy_breakdown[].avg_confidence`：
  - 长期低于 `0.45` 的策略应降权或停用

### 1.2 报警与看板
把以下指标接入监控系统（建议 5 分钟聚合）：

- 冲突率（全局 + 每 symbol/timeframe）
- 每策略有效信号率 `buy+sell / total`
- 每策略 missing_required 比例
- 不同 regime 的信号占比与胜率（后续接入 outcome）

### 1.3 信号质量日报
每日固定输出：

- Top 3 高冲突策略组合
- Top 3 高 hold_ratio 策略
- Top 3 置信度偏低策略
- 与前一日变化（delta）

---

## 2. 再做策略治理层（1~2 周）

### 2.1 策略分层与职责隔离
建议将策略分为三层并明确优先级：

1. **方向层（Trend/Bias）**：如 supertrend / ema_ribbon / macd_momentum
2. **时机层（Entry Timing）**：如 stoch_rsi / rsi_reversion / bollinger
3. **风控层（Risk Gate）**：如 regime、spread、economic guard

治理规则：

- 方向层和时机层不得直接“同级互斥”输出最终交易指令
- 最终执行由 voting/consensus 统一汇总
- 所有策略仅输出 `意图 + 置信度 + 解释`，交易执行器不直接耦合单策略

### 2.2 Regime 原生化开关
现在已有 `regime_affinity`，建议进一步做「策略-行情矩阵」配置化：

- 在 `config/signal.ini` 增加 per-strategy regime enabled/weight
- 支持运行时热加载（失败自动回滚到上一个快照）
- 出现高冲突时可快速只停用某 regime 下策略，而非全局停用

### 2.3 指标依赖契约化
降低 `missing_required_indicator` 发生频率：

- 启动时做策略依赖完整性检查（策略 required_indicators 是否全部可计算）
- 每次 indicator pipeline 变更自动回归策略依赖
- 为每个策略生成“最小可运行指标集合”文档

---

## 3. 然后做评估层（2~4 周）

### 3.1 Outcome Tracker 驱动校准闭环
将 `decision -> execution -> outcome` 打通到统一维度：

- 按 `strategy + action + regime + session` 统计胜率、盈亏比、MFE/MAE
- 每日更新 calibrator 参数（而不是人工拍脑袋调 confidence）
- 样本不足的分组自动回退到全局先验

### 3.2 Walk-forward + 分时段评估
黄金日内有明显时段特征，建议至少拆分：

- Asia / London / NY 三个 session
- 重大事件窗口（NFP/CPI/FOMC）单独评估

只要策略在某时段表现显著劣化，即可做“按 session 降权或禁用”。

### 3.3 冲突成本量化
不仅看冲突率，还要算冲突带来的成本：

- 信号反转时间（bar 内/跨 bar）
- 反向信号触发后的滑点与手续费影响
- 若冲突事件对应负收益显著高，优先治理该策略组合

---

## 4. 最后做系统结构升级（并行推进）

### 4.1 模块边界重构建议
将 `signals` 目录按职责进一步收敛成：

- `signals/strategies/`：纯策略逻辑（无 IO）
- `signals/orchestration/`：runtime、voting、state machine
- `signals/analytics/`：diagnostics、attribution、reporting
- `signals/execution/`：position_manager、executor adapter

收益：测试隔离更好，策略开发不会污染运行时状态机。

### 4.2 统一事件模型（Schema First）
当前 metadata 字段较灵活，建议定义版本化 schema：

- `SignalEventV1` / `SignalDecisionV1` 的必填字段与类型
- metadata 保留扩展位，但核心字段强约束
- schema 变更必须带版本迁移说明

### 4.3 Feature Flag + 灰度发布
每个策略支持：

- `enabled`
- `weight`
- `allowed_regimes`
- `allowed_sessions`
- `can_trade`（仅观测/可交易）

先观测后交易，避免新策略直接影响实盘。

---

## 5. 推荐执行顺序（可直接按 Sprint 跑）

### Sprint A（1 周）
- 完成诊断指标阈值固化 + 报警
- 建立日报模板
- 拉出“高冲突/高缺失”前 5 策略

### Sprint B（1~2 周）
- 策略分层治理（方向层/时机层/风控层）
- per-regime 开关与权重配置化
- 指标依赖契约检查

### Sprint C（2 周）
- outcome 闭环与自动校准
- 分 session 评估与按时段启停
- 冲突成本归因

### Sprint D（持续）
- 目录结构重构到 orchestration/analytics/execution
- 事件 schema versioning
- Feature Flag 灰度平台化

---

## 6. 你当前可以立即执行的 3 个动作

1. 固定每天 2 次拉取 `/signals/diagnostics/strategy-conflicts`，记录冲突率与 Top 异常策略。
2. 先下线 `hold_ratio` 高且 `avg_confidence` 低的策略，保留方向层核心策略。
3. 对 `missing_required_count` 高的策略优先修指标链路，不要先改交易阈值。

