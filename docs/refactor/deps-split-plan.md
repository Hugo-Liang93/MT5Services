# deps.py 拆分设计

> 目标：将 `src/api/deps.py`（652 行）从"全局单例 + 初始化 + 状态追踪 + getter"
> 拆分为三层，让 API、CLI、回测、测试都能复用构建逻辑而不共享全局状态。

---

## 1. 当前问题

### 1.1 职责过重

`deps.py` 当前承担 **四重职责**：

| # | 职责 | 行数 | 问题 |
|---|------|------|------|
| 1 | 容器定义（4 个子容器 + 属性代理） | ~200 行 | 23 个 property 代理仅为兼容 |
| 2 | 初始化编排（`_ensure_initialized`） | ~130 行 | 全局单例 + 双检锁 |
| 3 | 启动状态追踪 | ~50 行 | 与容器强耦合 |
| 4 | DI getter 函数 | ~120 行 | 21 个几乎相同的函数 |

### 1.2 全局单例限制

```python
_c = _Container()      # 进程内唯一
_initialized = False   # 一次性标志
```

- 无法在同一进程中创建第二个运行时实例（回测 worker、测试隔离）
- 回测被迫完全绕过 deps.py，使用独立的 `build_backtest_components()`
- 测试必须 mock 全局状态或使用独立工厂

### 1.3 build/start/stop 混杂

- `_ensure_initialized()` 只做 build（创建对象），不 start
- `lifespan` 中 start 各组件
- `shutdown_components()` 在 lifespan 中调用
- 但状态追踪（`_startup_status`）横跨三者

---

## 2. 目标架构

```
src/api/deps.py（瘦身）
  │ 仅保留 FastAPI DI 适配层
  │ getter 函数 + Depends() 注册
  │
  ↓ 委托给
src/app_runtime/
  ├── container.py     ← AppContainer：纯数据持有
  ├── builder.py       ← AppBuilder：构建所有组件（不启动线程）
  └── runtime.py       ← AppRuntime：start/stop/health/status
```

### 2.1 `AppContainer`（container.py）

**职责**：纯组件持有，无逻辑

```python
@dataclass
class AppContainer:
    """持有所有运行时组件的实例引用。"""
    # Market
    market_service: Optional[MarketDataService] = None
    storage_writer: Optional[StorageWriter] = None
    ingestor: Optional[BackgroundIngestor] = None
    indicator_manager: Optional[UnifiedIndicatorManager] = None

    # Signal
    signal_module: Optional[SignalModule] = None
    signal_runtime: Optional[SignalRuntime] = None
    htf_cache: Optional[HTFStateCache] = None
    signal_quality_tracker: Optional[SignalQualityTracker] = None
    calibrator: Optional[ConfidenceCalibrator] = None
    performance_tracker: Optional[StrategyPerformanceTracker] = None
    market_structure_analyzer: Optional[MarketStructureAnalyzer] = None

    # Trading
    trade_registry: Optional[TradingAccountRegistry] = None
    trade_module: Optional[TradingModule] = None
    trade_executor: Optional[TradeExecutor] = None
    position_manager: Optional[PositionManager] = None
    trade_outcome_tracker: Optional[TradeOutcomeTracker] = None
    pending_entry_manager: Optional[PendingEntryManager] = None

    # Calendar
    economic_calendar_service: Optional[EconomicCalendarService] = None

    # Monitoring
    health_monitor: Optional[HealthMonitor] = None
    monitoring_manager: Optional[MonitoringManager] = None
```

**特点**：
- 纯 dataclass，无 property 代理
- 无初始化逻辑
- 可序列化（用于诊断）
- 可以创建多个实例（回测、测试）

---

### 2.2 `AppBuilder`（builder.py）

**职责**：构建组件 + 连接依赖，**不启动任何线程**

```python
class AppBuilder:
    """构建完整的应用组件图。"""

    def build(self) -> AppContainer:
        """构建所有组件并返回容器。"""
        c = AppContainer()

        # 阶段 1-6（与当前 _ensure_initialized 相同）
        c.market_service = create_market_service(...)
        c.storage_writer = create_storage_writer(...)
        c.market_service.attach_storage(c.storage_writer)
        # ... 其余组件构建 ...

        return c

    def build_minimal(self, ...) -> AppContainer:
        """构建最小子集（仅指标计算，用于轻量测试）。"""
        ...

    def build_for_backtest(self, ...) -> AppContainer:
        """构建回测专用容器（合并当前 build_backtest_components 逻辑）。"""
        ...
```

**特点**：
- 每次调用返回全新 `AppContainer`
- 无全局状态
- 不启动任何后台线程
- 回测和实盘可复用同一个 builder（不同配置）

---

### 2.3 `AppRuntime`（runtime.py）

**职责**：管理容器内组件的 start/stop 生命周期

```python
class AppRuntime:
    """管理组件生命周期。"""

    def __init__(self, container: AppContainer):
        self.container = container
        self._status = StartupStatus()

    def start(self) -> None:
        """按序启动所有组件后台线程。"""
        self._status.phase = "starting"
        # storage_writer.start()
        # indicator_manager.start()
        # ingestor.start()
        # ...
        self._status.phase = "running"

    def stop(self) -> None:
        """按逆序关闭所有组件。"""
        self._status.phase = "stopping"
        # ... shutdown_components 逻辑 ...
        self._status.phase = "stopped"

    @property
    def status(self) -> dict:
        return self._status.to_dict()
```

**特点**：
- 持有 `AppContainer` 引用
- 管理启动状态追踪
- 与 `lifespan` 中的 start/stop 逻辑合并

---

### 2.4 `deps.py`（瘦身后）

```python
"""FastAPI DI 适配层 — 仅暴露 getter 和 lifespan。"""

_runtime: Optional[AppRuntime] = None

def _ensure_initialized() -> AppRuntime:
    global _runtime
    if _runtime is not None:
        return _runtime
    with _init_lock:
        if _runtime is not None:
            return _runtime
        container = AppBuilder().build()
        _runtime = AppRuntime(container)
        return _runtime

# 自动生成的 getter（或用元编程）
def get_market_service() -> MarketDataService:
    rt = _ensure_initialized()
    assert rt.container.market_service is not None
    return rt.container.market_service

# ... 其余 getter ...
```

**预期行数**：~150 行（从 652 行降至约 23%）

---

## 3. 实施步骤

### Step 1：创建 `src/app_runtime/` 骨架

```
src/app_runtime/
├── __init__.py
├── container.py     ← AppContainer dataclass
├── builder.py       ← 空壳，暂委托回 deps.py 逻辑
└── runtime.py       ← 空壳，暂委托回 lifespan 逻辑
```

### Step 2：迁移 AppContainer

- 从 `deps.py` 提取 `_Container` → `AppContainer`
- **去掉所有 property 代理**
- `deps.py` 改为持有 `AppContainer` 实例，更新所有字段访问路径

### Step 3：迁移 build 逻辑

- 从 `deps.py._ensure_initialized()` 提取构建代码 → `AppBuilder.build()`
- `deps.py._ensure_initialized()` 改为调用 `AppBuilder().build()`
- 现有工厂函数（`src/api/factories/`）不变

### Step 4：迁移 start/stop 逻辑

- 从 `lifespan.py` 提取 start/stop → `AppRuntime.start()/.stop()`
- `lifespan.py` 简化为创建 AppRuntime + 调用 start/stop
- 状态追踪（`_startup_status`）移入 `AppRuntime`

### Step 5：瘦身 deps.py

- 移除 `_Container` 类定义
- 移除 `_ensure_initialized()` 构建逻辑（保留 getter 入口）
- 移除状态追踪代码
- 最终仅保留 getter 函数 + lifespan 引用

### Step 6：回测集成

- `build_backtest_components()` 可选迁移为 `AppBuilder.build_for_backtest()`
- 或保持独立（当前回测工厂已经足够好）

---

## 4. 迁移策略

### 关键原则

1. **渐进式**：每步都能通过全部测试
2. **向后兼容**：`from src.api.deps import get_*` 签名不变
3. **不改工厂**：`src/api/factories/` 保持原样
4. **不改路由**：API 端点代码不动

### 风险控制

| 风险 | 缓解措施 |
|------|---------|
| 循环导入 | `app_runtime` 不导入 `api`，只导入 `factories` 和业务模块 |
| 全局状态残留 | `deps.py` 中 `_runtime` 变量替代 `_c` + `_initialized` |
| 测试破坏 | 现有测试不直接依赖 `_Container` 内部结构 |
| 回测兼容 | 回测工厂可选迁移，不强制 |

---

## 5. 验收标准

- [ ] `AppContainer` 是纯 dataclass，无逻辑方法
- [ ] `AppBuilder.build()` 返回完整容器，可在测试中独立调用
- [ ] `AppRuntime` 管理所有后台线程的 start/stop
- [ ] `deps.py` < 200 行
- [ ] 所有现有测试通过（`pytest -x`）
- [ ] 回测功能正常（`/v1/backtest/run`）
- [ ] 可在测试中创建独立的 AppContainer 实例
