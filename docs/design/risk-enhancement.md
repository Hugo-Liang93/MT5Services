# 风控增强设计文档 — PnL 熔断器 + PerformanceTracker 持久化恢复

> 状态：**已实现（2026-03-27）**
> 对应 commit：`e85e988`

---

## 1. 问题背景

### 1.1 PerformanceTracker 重启即归零

`StrategyPerformanceTracker` 是纯内存的日内绩效追踪器，维护 per-strategy 的 wins/losses/streak/PnL 状态。与之对比：

| 组件 | 持久化方式 | 重启后 |
|------|---------|--------|
| ConfidenceCalibrator | `dump()/load()` JSON 文件 | 从磁盘恢复 |
| PerformanceTracker（改造前）| 纯内存 | **完全归零** |
| PerformanceTracker（改造后）| 启动时从 DB 重放 | 恢复当天状态 |

重启归零的实际影响：当天已产生的 wins/losses/streak 全部丢失，置信度乘数回到初始值 1.0，日内学习效果消失。

### 1.2 连续亏损无暂停机制

现有 `TradeExecutor` 的熔断器（`circuit_breaker`）仅计 MT5 API 技术失败次数。真实亏损序列（如连续 5 笔止损）不触发任何暂停，策略会持续开仓直到资金耗尽或人工干预。

---

## 2. 解决方案

### 2.1 PerformanceTracker 重启恢复

**原理**：启动时从 DB 查询当天（过去 24h）的 signal_outcomes + trade_outcomes，按时间升序重放进 PerformanceTracker，恢复 wins/losses/streak 状态。镜像 ConfidenceCalibrator 的热启动模式。

**数据查询**（`signal_repo.fetch_recent_outcomes(hours=24)`）：

```sql
SELECT strategy, won, COALESCE(price_change, 0.0) AS pnl, regime, 'signal' AS source, recorded_at
FROM signal_outcomes
WHERE recorded_at >= NOW() - (24 * INTERVAL '1 hour') AND won IS NOT NULL
UNION ALL
SELECT strategy, won, COALESCE(price_change, 0.0) AS pnl, regime, 'trade' AS source, recorded_at
FROM trade_outcomes
WHERE recorded_at >= NOW() - (24 * INTERVAL '1 hour') AND won IS NOT NULL
ORDER BY recorded_at ASC
```

**重放方法**（`warm_up_from_db(rows)`）：
- 逐行调用 `record_outcome()` 恢复状态
- 重放过程同时触发 PnL 熔断评估（若重启前已触发熔断，重启后维持）
- 非致命：DB 不可用时 catch Exception → debug log，启动不受影响

**启动顺序**（`AppRuntime.start()`）：

```
_start_calibrator()           → 已有（JSON 缓存）
_start_performance_tracker()  → 新增（DB 重放）
_start_pending_entry()
_start_position_manager()
```

### 2.2 PnL 熔断器

**原理**：在 `PerformanceTracker` 中新增全局连续亏损计数器，`TradeExecutor` 在执行前独立检查，与技术熔断器并行运行。

**状态机**：

```
record_outcome(source="trade", won=False)
    → _global_trade_loss_streak += 1
    → 若 streak >= max_consecutive_losses:
        _pnl_circuit_paused = True
        _pnl_circuit_opened_at = time.monotonic()

record_outcome(source="trade", won=True)
    → _global_trade_loss_streak = 0

is_trading_paused()
    → 加锁检查 _pnl_circuit_paused
    → 超过 cooldown_minutes → 自动复位并返回 False
    → 否则返回 True

reset_session()
    → 同时重置 _global_trade_loss_streak / _pnl_circuit_paused / _pnl_circuit_opened_at
```

**TradeExecutor 检查位置**（`_handle_confirmed()` 内）：

```python
# GATE 2: 技术熔断（MT5 API 失败）
if self._circuit_open: ...

# GATE 2.5: PnL 熔断（实际亏损）
if self._performance_tracker and self._performance_tracker.is_trading_paused():
    self._notify_skip(event.signal_id, "pnl_circuit_paused", tf)
    return None
```

---

## 3. 配置

`config/signal.ini [pnl_circuit_breaker]`（由 `config/signal.py` 读取，以 `perf_tracker_pnl_circuit_` 前缀注入 `SignalConfig`）：

```ini
[pnl_circuit_breaker]
enabled = true
max_consecutive_losses = 5
cooldown_minutes = 120
```

对应 `SignalConfig` 字段：

```python
perf_tracker_pnl_circuit_enabled: bool = True
perf_tracker_pnl_circuit_max_consecutive_losses: int = 5
perf_tracker_pnl_circuit_cooldown_minutes: int = 120
```

---

## 4. 两个熔断器对比

| 维度 | 技术熔断（TradeExecutor） | PnL 熔断（PerformanceTracker） |
|------|--------------------------|-------------------------------|
| 触发条件 | MT5 API 调用失败 | 实际平仓亏损 |
| 默认阈值 | 3 次连续失败 | 5 次连续亏损 |
| 自动恢复 | 30 分钟 | 120 分钟 |
| 状态存储 | 内存（`_circuit_open`） | 内存（`_pnl_circuit_paused`）+ 重启从 DB 恢复 |
| INI section | `[circuit_breaker]` | `[pnl_circuit_breaker]` |
| 重置触发 | 手动 `reset_circuit()` 或超时 | 盈利一笔 or 超时 |

---

## 5. 涉及文件

| 文件 | 改动 |
|------|------|
| `src/persistence/repositories/signal_repo.py` | 新增 `fetch_recent_outcomes()` |
| `src/signals/evaluation/performance.py` | 新增 `warm_up_from_db()` / `is_trading_paused()` / PnL 状态字段 |
| `src/config/models/signal.py` | 新增 3 个 `perf_tracker_pnl_circuit_*` 字段 |
| `src/config/signal.py` | 读取 `[pnl_circuit_breaker]` section |
| `src/app_runtime/runtime.py` | 新增 `_start_performance_tracker()` 调用 |
| `src/app_runtime/factories/signals.py` | 传入 `performance_tracker` 到 TradeExecutor |
| `src/trading/signal_executor.py` | GATE 2.5 PnL 熔断检查 |
| `config/signal.ini` | 新增 `[pnl_circuit_breaker]` section |
| `config/backtest.ini` | `commission_per_lot = 7.0`，`slippage_points = 15.0` |
