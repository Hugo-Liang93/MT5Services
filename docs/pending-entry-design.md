# Pending Entry 价格确认入场机制 — 详细设计方案

## 1. 问题陈述

当前系统在 confirmed bar 收盘产生信号后**立即以市价下单**，成交价落在 Bar N+1 开盘附近。
Bar 切换时刻通常伴随流动性缺口和 spread 扩大，导致：
- 滑点偏大（开盘瞬间波动）
- 入场价不理想（追价）
- spread 成本偏高

## 2. 设计目标

将 "信号即下单" 改为 "**信号定方向 + Quote 价格确认入场**"：

```
Bar N 收盘 → confirmed 信号 + 计算入场价格区间 [entry_low, entry_high]
                ↓
Bar N+1 → Quote 持续监控 bid/ask（~0.5s 频率）
                ↓
        bid/ask 落入区间 AND spread 可接受 → 执行下单
        超时 / 价格远离 → 取消信号，记录原因
```

## 3. 核心概念：PendingEntry

```python
@dataclass
class PendingEntry:
    """一个等待价格确认的挂起入场意图。"""
    signal_event: SignalEvent           # 原始信号事件
    trade_params: TradeParameters       # 预计算的交易参数（SL/TP/仓位）
    cost_metrics: dict[str, Any]        # 预计算的成本指标

    # ── 入场区间 ──
    entry_low: float                    # 入场价格下限
    entry_high: float                   # 入场价格上限
    reference_price: float              # 信号 bar 收盘价（区间基准）

    # ── 生命周期 ──
    created_at: datetime                # 创建时间
    expires_at: datetime                # 超时时间
    status: str = "pending"             # pending / filled / expired / cancelled

    # ── 策略分类（决定区间逻辑）──
    zone_mode: str = "pullback"         # pullback / momentum / symmetric

    # ── 追踪 ──
    best_price_seen: float | None = None  # 监控期间见过的最优价
    checks_count: int = 0               # 检查次数
    fill_price: float | None = None     # 最终成交价（filled 后回填）
    cancel_reason: str = ""             # 取消/过期原因
```

## 4. 入场区间计算策略

### 4.1 三种 Zone Mode

根据策略 `category`（已有枚举 `StrategyCategory`）自动选择：

| StrategyCategory | zone_mode | 区间逻辑 | 适用场景 |
|-----------------|-----------|---------|---------|
| `trend` | **pullback** | 等回撤入场，区间偏向信号反方向 | 趋势跟踪策略希望在回调时入场 |
| `mean_reversion` | **symmetric** | 以 close 为中心对称区间 | 均值回归已在极值，给宽容度 |
| `breakout` | **momentum** | 允许顺势追一段，区间偏向信号方向 | 突破类追动量，但设上限 |
| `session` / `price_action` | **pullback** | 同趋势 | 基于收盘确认的策略 |
| `composite` / `consensus`（投票结果） | **symmetric** | 综合方向，对称区间 | 多策略投票，无单一偏好 |

### 4.2 区间计算公式

所有区间基于 ATR（已在 `TradeParameters.atr_value` 中可用）：

```python
# 配置参数（signal.ini [pending_entry]）
pullback_atr_factor: float = 0.3    # 回撤方向 ATR 倍数
chase_atr_factor: float = 0.1       # 追价方向 ATR 倍数（保守）
momentum_atr_factor: float = 0.5    # 顺势追价 ATR 倍数
symmetric_atr_factor: float = 0.4   # 对称区间半宽 ATR 倍数
```

#### Pullback 模式（趋势策略）

```
BUY 信号:
    entry_high = close + chase_atr × ATR     # 允许小幅上追（0.1×ATR）
    entry_low  = close - pullback_atr × ATR  # 等回撤（0.3×ATR）
    → 优先等价格回到 close 下方再入场

SELL 信号:
    entry_high = close + pullback_atr × ATR  # 等反弹（0.3×ATR）
    entry_low  = close - chase_atr × ATR     # 允许小幅下追（0.1×ATR）
    → 优先等价格回到 close 上方再入场
```

#### Momentum 模式（突破策略）

```
BUY 信号:
    entry_high = close + momentum_atr × ATR  # 允许顺势追（0.5×ATR）
    entry_low  = close - chase_atr × ATR     # 略微回撤也接受（0.1×ATR）

SELL 信号:
    entry_high = close + chase_atr × ATR
    entry_low  = close - momentum_atr × ATR
```

#### Symmetric 模式（均值回归 / 投票结果）

```
BUY/SELL 信号:
    entry_high = close + symmetric_atr × ATR
    entry_low  = close - symmetric_atr × ATR
    → 以 close 为中心对称
```

### 4.3 XAUUSD M5 实际数值示例

假设 ATR14 = 3.5（XAUUSD M5 典型值约 $3-5）

| 模式 | BUY 入场区间 | 宽度 |
|------|------------|------|
| pullback | [close-1.05, close+0.35] | $1.40 |
| momentum | [close-0.35, close+1.75] | $2.10 |
| symmetric | [close-1.40, close+1.40] | $2.80 |

> 对比：XAUUSD 点差通常 $0.20-0.50，M5 bar 振幅约 $2-5，区间设计在合理范围内。

## 5. 超时策略

按时间框架差异化，以 bar 周期为基准：

```python
# signal.ini [pending_entry]
# 超时 = bars × bar_duration
timeout_bars: dict[str, float] = {
    "M1":  3.0,    # 3 分钟
    "M5":  2.0,    # 10 分钟
    "M15": 1.5,    # 22.5 分钟
    "H1":  1.0,    # 60 分钟
    "H4":  0.5,    # 120 分钟
    "D1":  0.25,   # 6 小时
}
# 默认: 2.0 bars
default_timeout_bars: float = 2.0
```

## 6. 架构设计

### 6.1 新增组件：PendingEntryManager

```
src/trading/
├── signal_executor.py          # 现有（修改：不直接下单，创建 PendingEntry）
├── pending_entry.py            # 新增：PendingEntryManager
├── sizing.py                   # 现有（不变）
└── ...
```

### 6.2 PendingEntryManager 职责

```python
class PendingEntryManager:
    """管理挂起的入场意图，监控 Quote 价格确认后执行。"""

    def __init__(
        self,
        config: PendingEntryConfig,
        market_service: MarketDataService,
        execute_fn: Callable[[SignalEvent, TradeParameters, dict], Optional[dict]],
        on_expired_fn: Optional[Callable[[PendingEntry], None]] = None,
    ):
        self._config = config
        self._market = market_service
        self._execute_fn = execute_fn         # TradeExecutor._execute() 的引用
        self._on_expired_fn = on_expired_fn   # 过期回调（通知 QualityTracker）

        self._pending: dict[str, PendingEntry] = {}  # signal_id → PendingEntry
        self._lock = threading.Lock()
        self._monitor_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ── 公开接口 ──

    def submit(self, entry: PendingEntry) -> None:
        """提交一个新的挂起入场。"""
        ...

    def cancel(self, signal_id: str, reason: str = "manual") -> None:
        """取消指定的挂起入场。"""
        ...

    def cancel_by_symbol(self, symbol: str, reason: str = "new_signal_override") -> int:
        """取消指定品种的所有挂起入场（新信号覆盖旧信号）。"""
        ...

    def start(self) -> None:
        """启动价格监控线程。"""
        ...

    def shutdown(self) -> None:
        """停止监控并清理。"""
        ...

    def status(self) -> dict:
        """返回所有挂起入场的状态快照。"""
        ...

    # ── 内部逻辑 ──

    def _monitor_loop(self) -> None:
        """监控线程主循环，周期性检查所有 pending entry。"""
        while not self._stop_event.is_set():
            self._check_all_entries()
            self._stop_event.wait(timeout=self._config.check_interval)  # 默认 0.5s

    def _check_all_entries(self) -> None:
        """遍历所有 pending entry，检查价格和超时。"""
        now = datetime.utcnow()
        with self._lock:
            entries = list(self._pending.values())

        for entry in entries:
            if entry.status != "pending":
                continue

            # 1. 超时检查
            if now >= entry.expires_at:
                self._expire_entry(entry)
                continue

            # 2. 获取当前 quote
            quote = self._market.get_quote(entry.signal_event.symbol)
            if quote is None:
                continue

            # 3. 价格确认
            self._check_price(entry, quote)

    def _check_price(self, entry: PendingEntry, quote: Quote) -> None:
        """检查 bid/ask 是否落入入场区间。"""
        event = entry.signal_event
        entry.checks_count += 1

        # 根据方向取对应价格
        # BUY → 用 ask（实际买入价）
        # SELL → 用 bid（实际卖出价）
        check_price = quote.ask if event.action == "buy" else quote.bid

        # 更新 best_price_seen
        if entry.best_price_seen is None:
            entry.best_price_seen = check_price
        elif event.action == "buy":
            entry.best_price_seen = min(entry.best_price_seen, check_price)
        else:
            entry.best_price_seen = max(entry.best_price_seen, check_price)

        # 检查是否在入场区间内
        if entry.entry_low <= check_price <= entry.entry_high:
            # 再检查 spread
            spread_points = self._get_spread_points(quote, event.symbol)
            if spread_points is not None and spread_points > self._config.max_spread_points:
                return  # spread 过大，继续等待

            self._fill_entry(entry, check_price)

    def _fill_entry(self, entry: PendingEntry, fill_price: float) -> None:
        """价格确认，执行下单。"""
        entry.status = "filled"
        entry.fill_price = fill_price

        with self._lock:
            self._pending.pop(entry.signal_event.signal_id, None)

        # 调用 TradeExecutor._execute()
        self._execute_fn(
            entry.signal_event,
            entry.trade_params,
            entry.cost_metrics,
        )

    def _expire_entry(self, entry: PendingEntry) -> None:
        """入场意图超时。"""
        entry.status = "expired"
        entry.cancel_reason = "timeout"

        with self._lock:
            self._pending.pop(entry.signal_event.signal_id, None)

        if self._on_expired_fn:
            self._on_expired_fn(entry)
```

### 6.3 TradeExecutor 修改

修改 `_handle_confirmed()` 的最后一步——**不再直接调用 `_execute()`，而是创建 PendingEntry 提交给 PendingEntryManager**：

```python
# 当前代码（signal_executor.py _handle_confirmed 尾部）:
#   result = self._execute(event, trade_params, cost_metrics=cost_metrics)

# 修改后:
if self._pending_manager is not None and self._pending_manager.config.enabled:
    # 创建 PendingEntry，走价格确认流程
    pending = self._build_pending_entry(event, trade_params, cost_metrics)

    # 取消同品种的旧 pending（新信号覆盖）
    self._pending_manager.cancel_by_symbol(event.symbol, reason="new_signal_override")

    self._pending_manager.submit(pending)
    logger.info(
        "PendingEntry created: %s/%s %s zone=[%.2f, %.2f] timeout=%s",
        event.symbol, event.strategy, event.action,
        pending.entry_low, pending.entry_high,
        pending.expires_at.isoformat(),
    )
else:
    # 降级：直接执行（兼容旧行为）
    result = self._execute(event, trade_params, cost_metrics=cost_metrics)
```

### 6.4 完整数据流

```
SignalRuntime → publish_signal_event(confirmed_buy/sell)
    ↓
TradeExecutor.on_signal_event()
    ↓ 入队
_handle_confirmed()
    │
    ├─ 熔断器 ✓
    ├─ ExecutionGate ✓
    ├─ 置信度 ✓
    ├─ 持仓限制 ✓
    ├─ 计算 TradeParameters ✓
    ├─ 成本预检 ✓
    │
    ├─ [pending_entry enabled]
    │   ↓
    │   _build_pending_entry() → PendingEntry
    │   ↓
    │   cancel_by_symbol() → 取消旧 pending
    │   ↓
    │   PendingEntryManager.submit(pending)
    │   ↓
    │   _monitor_loop()（后台线程，~0.5s 周期）
    │   ↓
    │   get_quote(symbol) → bid/ask
    │   ↓
    │   ask/bid ∈ [entry_low, entry_high] AND spread OK
    │       ↓ YES
    │   TradeExecutor._execute(event, params, cost_metrics)
    │       ↓
    │   TradingModule → MT5 下单
    │
    └─ [pending_entry disabled / 降级]
        ↓
        TradeExecutor._execute()  ← 原始直接下单路径
```

## 7. 配置方案

### 7.1 signal.ini 新增 section

```ini
[pending_entry]
# 总开关
enabled = true

# ── 价格监控 ──
check_interval = 0.5                # 价格检查周期（秒），对齐 quote 采集频率
max_spread_points = 50.0            # pending 期间 spread 上限（0=不检查，复用 signal_filters）

# ── 超时（单位：bars 数量）──
timeout_bars_M1 = 3.0
timeout_bars_M5 = 2.0
timeout_bars_M15 = 1.5
timeout_bars_H1 = 1.0
timeout_bars_H4 = 0.5
timeout_bars_D1 = 0.25
default_timeout_bars = 2.0

# ── 入场区间 ATR 倍数 ──
# Pullback 模式（趋势策略）
pullback_atr_factor = 0.3           # 回撤方向
chase_atr_factor = 0.1              # 追价方向

# Momentum 模式（突破策略）
momentum_atr_factor = 0.5           # 顺势方向

# Symmetric 模式（均值回归 / 投票）
symmetric_atr_factor = 0.4          # 半宽

# ── 策略级覆盖（可选）──
# 格式：策略名__参数名 = 值
# supertrend__pullback_atr_factor = 0.4
# rsi_reversion__symmetric_atr_factor = 0.5

# ── 新信号覆盖行为 ──
cancel_on_new_signal = true         # 同品种新信号取消旧 pending
# 同品种同方向新信号是否覆盖
cancel_same_direction = false       # false = 同方向新信号不覆盖，保留更优的旧 pending
```

### 7.2 PendingEntryConfig 数据类

```python
@dataclass
class PendingEntryConfig:
    enabled: bool = False
    check_interval: float = 0.5
    max_spread_points: float = 50.0

    # 超时
    timeout_bars: dict[str, float] = field(default_factory=lambda: {
        "M1": 3.0, "M5": 2.0, "M15": 1.5,
        "H1": 1.0, "H4": 0.5, "D1": 0.25,
    })
    default_timeout_bars: float = 2.0

    # 区间参数
    pullback_atr_factor: float = 0.3
    chase_atr_factor: float = 0.1
    momentum_atr_factor: float = 0.5
    symmetric_atr_factor: float = 0.4

    # 信号覆盖
    cancel_on_new_signal: bool = True
    cancel_same_direction: bool = False

    # 策略级覆盖
    strategy_overrides: dict[str, dict[str, float]] = field(default_factory=dict)
```

## 8. 边界情况处理

### 8.1 新信号覆盖旧 Pending

```
Bar N 收盘 → BUY 信号 → PendingEntry A（等待中）
Bar N+1 收盘 → SELL 信号 → 取消 A，创建 PendingEntry B
```

- `cancel_on_new_signal = true`：同品种新信号取消所有旧 pending
- `cancel_same_direction = false`：同方向新信号不覆盖（避免重复）

### 8.2 价格瞬间穿越区间

Quote 采集频率 ~0.5s，可能错过区间内的瞬间价格。
**可接受**：如果价格在 0.5s 内穿过区间，说明动量过强，不入场反而更安全。

### 8.3 服务重启

- PendingEntry 是纯内存状态，重启后丢失
- **符合预期**：挂起时间短（几分钟），重启后市场状态已变，旧意图不应恢复

### 8.4 多时间框架同品种信号

- 同一品种不同 timeframe 可能产生不同方向信号
- PendingEntry 按 `signal_id` 唯一，`cancel_by_symbol()` 取消所有 timeframe 的旧 pending
- 与当前 `max_concurrent_positions_per_symbol` 限制一致

### 8.5 Pending 期间新 Bar 收盘

- 新 bar 收盘 → 新信号覆盖 → 旧 pending 被取消
- 如果新 bar 无信号（hold），旧 pending 继续等待直到超时
- 可选：收到 `confirmed_cancelled` 时主动取消（后续迭代）

### 8.6 降级模式

当 `pending_entry.enabled = false` 时，行为完全等同于当前代码——信号直接下单，零风险。

## 9. 可观测性

### 9.1 日志

```
INFO  PendingEntry created: XAUUSD/supertrend buy zone=[2650.30, 2651.40] timeout=2025-12-01T10:10:00
INFO  PendingEntry filled: XAUUSD/supertrend buy @ ask=2650.85 (ref=2651.00, improve=$0.15)
INFO  PendingEntry expired: XAUUSD/supertrend buy best_seen=2652.30 (zone_high=2651.40) checks=20
WARN  PendingEntry spread too wide during fill: XAUUSD spread=65pts > max=50pts, skip this check
```

### 9.2 状态 API

在 `TradeExecutor.status()` 中扩展：

```python
{
    "pending_entries": {
        "active_count": 1,
        "entries": [
            {
                "signal_id": "...",
                "symbol": "XAUUSD",
                "action": "buy",
                "strategy": "supertrend",
                "zone": [2650.30, 2651.40],
                "reference_price": 2651.00,
                "zone_mode": "pullback",
                "created_at": "...",
                "expires_at": "...",
                "checks_count": 15,
                "best_price_seen": 2651.80,
                "remaining_seconds": 45.2,
            }
        ],
        "stats": {
            "total_submitted": 120,
            "total_filled": 85,
            "total_expired": 30,
            "total_cancelled": 5,
            "avg_wait_seconds": 12.5,
            "avg_price_improvement": 0.35,  # 相比 reference_price 的平均优化
            "fill_rate": 0.708,             # 85/120
        }
    }
}
```

### 9.3 持久化（可选，后续迭代）

PendingEntry 的结果（filled/expired/cancelled）可写入 StorageWriter 的 auto_executions 通道，
附加字段：`wait_seconds`, `price_improvement`, `zone_mode`, `checks_count`。
用于后续分析入场区间参数的最优化。

## 10. 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `src/trading/pending_entry.py` | **新增** | PendingEntryManager + PendingEntry + PendingEntryConfig |
| `src/trading/signal_executor.py` | **修改** | `_handle_confirmed()` 尾部分支 + `_build_pending_entry()` 方法 |
| `src/api/factories/signals.py` | **修改** | 工厂函数创建 PendingEntryManager 并注入 TradeExecutor |
| `src/api/lifespan.py` | **修改** | shutdown 中加入 PendingEntryManager.shutdown() |
| `src/api/deps.py` | **修改** | `_TradingContainer` 新增 `pending_entry_manager` 字段 |
| `src/config/signal.py` | **修改** | 解析 `[pending_entry]` section |
| `config/signal.ini` | **修改** | 新增 `[pending_entry]` section（默认 `enabled = false`） |
| `tests/trading/test_pending_entry.py` | **新增** | PendingEntryManager 单元测试 |

## 11. 实现优先级

### Phase 1：核心功能（本次实现）
1. PendingEntry 数据类 + PendingEntryConfig
2. PendingEntryManager 核心逻辑（submit / monitor / fill / expire）
3. 三种 zone mode 区间计算
4. TradeExecutor 集成（分支 + 降级）
5. signal.ini 配置加载
6. 工厂函数 + DI 注入
7. 单元测试

### Phase 2：增强（后续迭代）
- 策略级 ATR factor 覆盖
- confirmed_cancelled 主动取消 pending
- 持久化统计（fill_rate / avg_improvement）
- API 端点暴露 pending 状态
- 热重载支持

### Phase 3：优化（数据驱动）
- 基于历史 fill_rate 自动调整区间宽度
- 基于 regime 动态调整超时
- 基于时段（伦敦/纽约）差异化参数
