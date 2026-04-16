# 可观测性增强设计

> 状态：**IMPLEMENTED**  
> 创建：2026-04-16  
> 关联：`src/monitoring/`, `src/trading/execution/executor.py`, `src/trading/positions/manager.py`

---

## 1. 问题诊断

当前监控体系存在**四个关键盲区**，导致生产环境中的异常状态无法及时感知：

| 盲区 | 影响 | 当前覆盖 |
|------|------|---------|
| Reconciliation 延迟 | 持仓与 broker 脱节但无人知晓 | **无** |
| 熔断器状态 | 自动交易暂停但运维不知 | executor.status() 有数据但未接入告警 |
| 跨实例健康聚合 | 单实例监控，无全局视图 | **无** |
| 交易执行质量指标 | 执行失败/队列溢出无告警 | 仅日志，无指标 |

**根因**：`MonitoringManager` 当前只注册了 5 类检查方法（data_latency / indicator_freshness / queue_stats / cache_stats / performance_stats），**交易域的关键指标未接入监控循环**。

---

## 2. 设计方案

### 2.1 新增 4 个交易域监控指标

在 `MonitoringManager._monitoring_loop` 中新增交易域检查，利用已有的 `register_component` + `status()` 模式。

#### 2.1.1 Reconciliation 延迟指标

**数据源**：`PositionManager.status()` 已暴露 `last_reconcile_at`（`manager.py:445-447`）。

```python
# monitoring/manager.py — _monitoring_loop 中新增

elif method == "reconciliation_lag" and hasattr(component_obj, "status"):
    pm_status = component_obj.status()
    last_at = pm_status.get("last_reconcile_at")
    if last_at is not None:
        try:
            last_dt = datetime.fromisoformat(last_at)
            lag = (datetime.now(timezone.utc) - last_dt).total_seconds()
            self.health_monitor.record_metric(
                name,
                "reconciliation_lag",
                lag,
                {
                    "last_reconcile_at": last_at,
                    "reconcile_count": pm_status.get("reconcile_count"),
                    "tracked_positions": pm_status.get("tracked_positions"),
                    "last_error": pm_status.get("last_error"),
                },
            )
        except (TypeError, ValueError):
            pass
```

**告警阈值**：

```python
# health/monitor.py — alerts 字典新增
"reconciliation_lag": {"warning": 30.0, "critical": 120.0},
```

- warning 30s：reconcile 延迟超过 3 个周期（正常 10s）
- critical 120s：reconcile 线程可能已死

**注册方式**：

```python
# runtime.py — 启动时注册
c.monitoring_manager.register_component(
    "position_manager", c.position_manager, ["reconciliation_lag"]
)
```

#### 2.1.2 熔断器状态指标

**数据源**：`TradeExecutor.status()` 已暴露完整的 circuit breaker 状态（`executor.py:1204-1211`）。

```python
# monitoring/manager.py — _monitoring_loop 中新增

elif method == "circuit_breaker" and hasattr(component_obj, "status"):
    exec_status = component_obj.status()
    cb = exec_status.get("circuit_breaker", {})
    is_open = 1.0 if cb.get("open") else 0.0
    self.health_monitor.record_metric(
        name,
        "circuit_breaker_open",
        is_open,
        {
            "consecutive_failures": cb.get("consecutive_failures"),
            "circuit_open_at": cb.get("circuit_open_at"),
            "health_check_failures": cb.get("health_check_failures"),
        },
    )
```

**告警阈值**：

```python
"circuit_breaker_open": {"warning": 0.5, "critical": 0.5},
# 值为 1.0 时 >= 0.5 触发 critical（熔断 = 立即告警）
```

**注册方式**：

```python
c.monitoring_manager.register_component(
    "trade_executor", c.trade_executor, ["circuit_breaker"]
)
```

#### 2.1.3 执行质量指标

**数据源**：`TradeExecutor.status()` 已暴露 `execution_stats`。

```python
elif method == "execution_quality" and hasattr(component_obj, "status"):
    exec_status = component_obj.status()
    stats = exec_status.get("execution_stats", {})
    total = int(stats.get("total_processed", 0) or 0)
    failed = int(stats.get("total_failed", 0) or 0)
    if total > 0:
        failure_rate = failed / total
        self.health_monitor.record_metric(
            name,
            "execution_failure_rate",
            failure_rate,
            stats,
        )
    overflows = int(stats.get("queue_overflows", 0) or 0)
    if overflows > 0:
        self.health_monitor.record_metric(
            name,
            "execution_queue_overflows",
            float(overflows),
            stats,
        )
```

**告警阈值**：

```python
"execution_failure_rate": {"warning": 0.1, "critical": 0.3},
"execution_queue_overflows": {"warning": 1.0, "critical": 5.0},
```

#### 2.1.4 持仓数量异常指标

```python
elif method == "position_count" and hasattr(component_obj, "status"):
    pm_status = component_obj.status()
    count = int(pm_status.get("tracked_positions", 0) or 0)
    self.health_monitor.record_metric(
        name,
        "tracked_position_count",
        float(count),
        pm_status,
    )
```

不设告警阈值——仅记录指标，用于趋势观察和异常回溯。

### 2.2 告警阈值注册扩展

在 `HealthMonitor.__init__` 和 `_check_alert_level` 中统一处理新指标：

```python
# health/monitor.py — alerts 字典完整新增项

self.alerts.update({
    "reconciliation_lag": {"warning": 30.0, "critical": 120.0},
    "circuit_breaker_open": {"warning": 0.5, "critical": 0.5},
    "execution_failure_rate": {"warning": 0.1, "critical": 0.3},
    "execution_queue_overflows": {"warning": 1.0, "critical": 5.0},
})
```

`_check_alert_level` 中为新指标添加比较方向（值越大越危险）：

```python
# health/monitor.py — _check_alert_level 扩展

elif metric_name in {
    "reconciliation_lag",
    "circuit_breaker_open",
    "execution_failure_rate",
    "execution_queue_overflows",
}:
    if value >= thresholds["critical"]:
        return "critical"
    if value >= thresholds["warning"]:
        return "warning"
```

### 2.3 PositionManager.status() 扩展

在现有 `status()` 方法中补充 flush 相关信息（配合 position-state-consistency 设计）：

```python
# manager.py — status() 方法扩展

def status(self) -> Dict[str, Any]:
    with self._lock:
        position_count = len(self._positions)
        dirty_count = sum(1 for p in self._positions.values() if getattr(p, '_dirty', False))
    # ... 现有内容 ...
    result = {
        # ... 现有字段 ...
        "dirty_positions": dirty_count,  # 新增：待 flush 的脏位数
    }
    return result
```

### 2.4 跨实例健康聚合（Phase 2）

**方案**：在 Supervisor 层添加轻量 health aggregator，周期性 HTTP 请求每个实例的 `/health` 端点。

```python
# entrypoint/supervisor.py — 新增方法

async def _aggregate_health(self) -> dict[str, Any]:
    """聚合所有实例的健康状态。"""
    results = {}
    for instance_name, managed in self._managed.items():
        if managed.process.poll() is not None:
            results[instance_name] = {"status": "dead", "exit_code": managed.process.returncode}
            continue
        try:
            port = self._resolve_port(instance_name)
            resp = requests.get(f"http://127.0.0.1:{port}/health", timeout=3)
            results[instance_name] = resp.json()
        except Exception as exc:
            results[instance_name] = {"status": "unreachable", "error": str(exc)}
    return {
        "group": self._group.name,
        "instances": results,
        "all_healthy": all(
            r.get("status") == "healthy" for r in results.values()
        ),
    }
```

**暴露方式**：Supervisor 自身不跑 FastAPI，可通过以下方式暴露：
- 方案 A：写 JSON 到 `data/runtime/supervisor_health.json`，QuantX 前端读取
- 方案 B：Supervisor 启动一个最小化 HTTP 服务（仅 1 个端点）
- **推荐方案 A**——最简单，无额外依赖

### 2.5 熔断器手动重置 API

当前 `TradeExecutor` 已有 `reset_circuit()` 方法（`executor.py:650-656`），但**没有 API 路由暴露**。

```python
# api/trade_routes/command_routes/direct.py — 新增路由

@router.post("/v1/trading/circuit/reset")
async def reset_circuit_breaker(
    executor: TradeExecutor = Depends(get_trade_executor),
) -> ApiResponse:
    """手动重置交易熔断器。"""
    executor.reset_circuit(event="manual_reset", reason="operator_api_call")
    return ApiResponse(data={"circuit_open": executor.circuit_open})
```

---

## 3. 实施计划

| 阶段 | 内容 | 改动文件 | 工作量 |
|------|------|---------|--------|
| **Phase 1a** | 4 个新指标注册 + 告警阈值 | `monitoring/manager.py`, `health/monitor.py` | ~80 行 |
| **Phase 1b** | `_check_alert_level` 扩展 | `health/monitor.py` | ~15 行 |
| **Phase 1c** | runtime.py 注册 position_manager + trade_executor | `app_runtime/runtime.py` | ~10 行 |
| **Phase 2a** | Supervisor health aggregator | `entrypoint/supervisor.py` | ~40 行 |
| **Phase 2b** | 熔断器重置 API | `api/trade_routes/` | ~15 行 |

---

## 4. 效果预期

### 改进前

```
问题发生 → 日志中有 WARNING → 无人看日志 → 持仓脱节/交易暂停数小时 → 事后回溯
```

### 改进后

```
问题发生 → HealthMonitor 记录指标 → 触发告警 → active_alerts 暴露到 /health API
         → QuantX 前端/SSE 流推送 → 运维即时响应
         → 熔断器可通过 API 手动重置
```

### 关键 SLA 目标

| 指标 | 目标 | 当前 |
|------|------|------|
| Reconciliation 异常发现时间 | < 60s | 无监控 |
| 熔断器状态感知延迟 | < 60s | 仅日志 |
| 执行队列溢出告警 | 首次溢出即告警 | 无 |
| 跨实例状态感知 | < 3min | 无 |

---

## 5. 不做什么

- **不做 Prometheus / Grafana 集成**：当前规模用内存 ring buffer + SQLite alert 足够，外部监控系统在多实例 > 5 时再考虑
- **不做日志聚合**：每实例独立日志文件，QuantX 前端通过 API 读取
- **不改 MonitoringManager 架构**：现有 60s 轮询 + register_component 模式足够扩展，无需事件驱动改造
- **不做告警通知推送**（邮件/钉钉/Slack）：当前单人运维，QuantX 前端轮询 `/health` 即可
