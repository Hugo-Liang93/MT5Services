# Next Plan — 下一阶段开发规划

> 更新日期：2026-04-01
> 基于当前系统状态与已实现功能制定。

---

## 系统现状总结

已实现的核心能力：

| 能力 | 状态 |
|------|------|
| 信号生成（30+ 策略 + 5 复合 + 4 投票组） | ✅ 生产就绪 |
| 跨 TF 策略体系（HTFTrendPullback / DualTFMomentum） | ✅ M30 已验证盈利 |
| M5 专用 intrabar 策略（ScalpRSI / MomentumBurst） | ✅ 已实现，待验证 |
| 置信度管线（Regime × Perf × Calibrator × HTF） | ✅ 生产就绪 |
| 价格确认入场（PendingEntry 3 种模式） | ✅ 生产就绪 |
| 技术熔断器（MT5 API 失败计数） | ✅ 生产就绪 |
| PnL 熔断器（实际亏损连续计数） | ✅ 已实现 |
| PerformanceTracker 重启恢复 | ✅ 已实现 |
| 回测引擎（per-strategy session + voting groups + 真实成本） | ✅ 已实现 |
| Walk-Forward + 参数推荐 | ✅ 已实现 |
| Studio 可观测性（14 Agent SSE） | ✅ 已实现 |
| 日终自动平仓 | ✅ 已实现 |
| 同向再入场冷却（reentry_cooldown_bars） | ✅ 已实现 |
| 同向叠加控制 | ⚠️ 仅 per-symbol 持仓数限制 + 冷却 bar，无净敞口控制 |
| 盈亏比动态调整 | ⚠️ ATR 倍数按 TF 差异化 + Regime 缩放，但仍需优化 |
| Indicator Exit | ⚠️ 已禁用（A/B 实验证明误触发率过高） |
| 权益曲线过滤 | ❌ 未实现 |
| 经济日历 Trade Guard | ✅ 已实现 |

---

## Phase 2：中等复杂度（推荐下一步实施）

### 2A. Regime-Aware 动态 TP/SL 倍数

**问题**：`sizing.py` 中 SL/TP ATR 倍数按 TF 固定，不随市场 Regime 调整。RANGING 市场 TP 过大会被反转打止损；TRENDING 市场 TP 过小提前止盈浪费空间。

**方案**：

```python
# sizing.py compute_trade_params() 接收 regime 参数
REGIME_TP_MULTIPLIER = {
    RegimeType.TRENDING:  1.20,   # TP 扩大 20%，让趋势充分运行
    RegimeType.RANGING:   0.80,   # TP 收紧 20%，避免被均值回归打回
    RegimeType.BREAKOUT:  1.10,
    RegimeType.UNCERTAIN: 1.00,
}
REGIME_SL_MULTIPLIER = {
    RegimeType.TRENDING:  1.00,
    RegimeType.RANGING:   0.90,   # SL 收紧，震荡市止损不宜过宽
    RegimeType.BREAKOUT:  1.10,   # 突破需要更大止损空间
    RegimeType.UNCERTAIN: 1.00,
}
```

**涉及文件**：`src/trading/sizing.py`、`src/backtesting/portfolio.py`（同步更新）
**配置**：`signal.ini [regime_sizing]` 可覆盖倍数
**复杂度**：低（sizing 是纯函数，加参数即可）

---

### 2B. 跨 TF 净敞口控制

**问题**：单品种最多 3 仓，但可能同时在 M5/H1/H4 各开 3 仓同向，实际净敞口远超预期。`AccountSnapshotRule` 只检查总仓数，不计净手数。

**方案**：在 `AccountSnapshotRule`（`src/risk/rules.py`）新增净手数检查：

```python
# 同一品种同方向总手数 ≤ max_net_lots_per_symbol (默认 0.3 手)
net_lots = sum(pos.volume for pos in same_symbol_same_dir_positions)
if net_lots + new_volume > config.max_net_lots_per_symbol:
    return RiskCheckResult.reject("NET_LOTS_LIMIT", ...)
```

**涉及文件**：`src/risk/rules.py`、`config/risk.ini`（新增 `max_net_lots_per_symbol`）
**复杂度**：低（在现有规则框架内扩展）

---

### 2C. Equity Curve Filter（权益曲线过滤）

**问题**：当账户权益持续下行时，系统仍持续开新仓。缺乏基于资金曲线走势的动态开关。

**方案**：在 `SignalFilterChain` 新增 `EquityCurveFilter`：

```
原理: 计算近 N 笔交易（或近 N 天）权益移动均线
      当前权益 < MA × (1 - drawdown_threshold) → 触发过滤
      权益恢复到 MA 以上 → 自动解除
```

**数据来源**：`TradingModule.get_account_info()` 实时查询（每次过滤前调用，或后台线程定时更新缓存）

**涉及文件**：
- `src/signals/execution/filters.py` — 新增 `EquityCurveFilter`
- `config/signal.ini` — 新增 `[equity_curve_filter]` section
- `src/trading/service.py` — 暴露权益历史查询接口

**复杂度**：中（需要后台线程维护权益 MA，或每次过滤时实时查询）

---

### 2D. 回测结果数据库持久化增强

**问题**：Walk-Forward 结果存内存缓存，API 重启后丢失；无法跨 session 对比历史回测。

**当前状态**：⚠️ 部分已实现 — `backtest_repo.py`（459 行）已有 `save_result()` / `save_recommendation()` 等方法，回测结果和推荐记录可持久化到 DB。Walk-Forward splits 的 DB 持久化和重启恢复仍为待做。

**剩余工作**：

```
Walk-Forward 结果 → 写入 backtest_wf_splits 表（待做）
API 重启后 → 从 DB 查询历史 WF 结果 list（待做）
```

**涉及文件**：`src/persistence/repositories/backtest_repo.py`、`src/backtesting/api.py`

**复杂度**：低（repo 框架已就绪，补全 WF 相关写入/查询）

---

## Phase 3：高复杂度（后续规划）

### 3A. 多品种支持扩展

**现状**：系统以 XAUUSD 为主，合约规格、点值、点差范围硬编码在多处。

**方案**：`contract_size_map`（已有）扩展为完整的品种规格配置，含点值、最小手数、保证金模式。

---

### 3B. 实盘参数热重载

**现状**：`[regime_detector]` / `[strategy_params]` 修改后需重启服务。

**方案**：API 端点 `POST /v1/config/reload`，触发 `get_signal_config.cache_clear()` + 各组件重新读取配置，无需重启。

---

### 3C. 策略级仓位追踪与绩效归因

**现状**：`trade_outcomes` 表有 `strategy` 字段，但缺少按策略维度的实时仓位收益追踪。

**方案**：`PositionManager` 在 `track_position()` 时记录策略来源，`TradeOutcomeTracker` 按 strategy/regime/tf 维度汇总月度盈亏归因报告。

---

### 3D. 多账户并行运营

**现状**：`TradingAccountRegistry` 支持注册多账户，但信号分发和风控聚合仅针对单账户。

---

## 验收标准参考

| 功能 | 验收条件 |
|------|---------|
| 2A Regime-Aware TP/SL | 回测显示 RANGING 环境盈亏比提升；Sharpe 无显著下降 |
| 2B 净敞口控制 | 单品种同方向手数上限在 precheck 中被拦截，日志可见 |
| 2C 权益曲线过滤 | 模拟连续亏损 3 笔后信号被过滤；权益恢复后自动解除 |
| 2D WF 结果持久化 | API 重启后 `/v1/backtest/results` 仍可查询历史 WF 结果 |

---

## 技术债清单

| 项目 | 位置 | 优先级 |
|------|------|--------|
| `manager.py` ~12 个单行转发包装器 | `src/indicators/manager.py` | 低 |
| 旧路由无前缀兼容层 | `src/api/__init__.py` | 低 |
| `get_ohlc()` 别名 | `src/market/service.py` | 低 |
| WF 结果内存缓存 | `src/backtesting/api.py` | 中（见 2D）|
| `SignalRuntime` 仍有 1,409 行 | `src/signals/orchestration/runtime.py` | 中 |
