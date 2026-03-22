# HTF 指标缓存系统设计方案

## 问题

当前 `HTFStateCache` 仅缓存高时间框架的**信号方向**（buy/sell/hold），不缓存指标值。
策略无法在评估时访问其他时间框架的指标数据，导致以下场景不可实现：

1. **跨 TF 指标比较**：如 H1 策略需要 H4 的 RSI/ADX 值做趋势确认
2. **Intrabar 引用 confirmed 指标**：如 intrabar 策略需要上一次 confirmed 快照中的 EMA 值
3. **多维度共同确认**：如 M5 策略同时参考 H1 趋势方向 + H4 波动率状态

---

## 设计原则

1. **需求驱动**：仅计算策略声明需要的 HTF 指标（与 intrabar 指标推导机制一致）
2. **被动缓存**：不主动拉取 HTF 数据，而是监听已有的 confirmed 快照并缓存
3. **零侵入**：不改变 `SignalStrategy.evaluate()` 的签名，通过 `SignalContext.metadata` 传递
4. **TTL 过期**：HTF 指标有时效性，过期后不提供（避免陈旧数据误导决策）

---

## 核心组件：`HTFIndicatorCache`

### 职责

替代/升级现有 `HTFStateCache`，同时缓存**信号方向**和**指标快照**。

### 数据结构

```python
class HTFIndicatorCache:
    """高时间框架指标缓存。

    监听 IndicatorManager 的 confirmed 快照，
    按 (symbol, timeframe) 缓存最新指标值。
    策略通过声明 htf_indicators 获取跨时间框架数据。
    """

    # 缓存结构：(symbol, timeframe) → HTFCacheEntry
    _indicator_cache: Dict[Tuple[str, str], HTFCacheEntry]

    # 信号方向缓存（兼容现有 HTFStateCache 功能）
    _direction_cache: Dict[Tuple[str, str], Tuple[str, datetime]]


@dataclass
class HTFCacheEntry:
    indicators: Dict[str, Dict[str, float]]  # 指标名 → {字段: 值}
    bar_time: datetime                         # 该快照对应的 K 线时间
    updated_at: datetime                       # 缓存写入时间
```

### 缓存写入：监听 confirmed 快照

```python
def on_confirmed_snapshot(
    self,
    symbol: str,
    timeframe: str,
    bar_time: datetime,
    indicators: Dict[str, Dict[str, float]],
    scope: str,
) -> None:
    """IndicatorManager 的 snapshot_listener 回调。

    仅缓存 scope="confirmed" 的快照（收盘数据可靠）。
    仅缓存 _eligible_indicators 中的指标（需求驱动）。
    """
    if scope != "confirmed":
        return
    eligible = self._eligible_indicators.get(timeframe, frozenset())
    if not eligible:
        return
    filtered = {
        name: payload
        for name, payload in indicators.items()
        if name in eligible
    }
    if not filtered:
        return
    key = (symbol, timeframe)
    with self._lock:
        self._indicator_cache[key] = HTFCacheEntry(
            indicators=filtered,
            bar_time=bar_time,
            updated_at=datetime.now(timezone.utc),
        )
```

> **关键**：confirmed 快照本来就会对所有 (symbol, tf) 发布，这里只是"接住"并缓存，
> 不需要额外触发计算。

### 缓存读取：策略评估时注入

```python
def get_htf_indicators(
    self,
    symbol: str,
    ltf_timeframe: str,
    required: Mapping[str, Tuple[str, ...]] | None = None,
) -> Dict[str, Dict[str, Dict[str, float]]]:
    """返回 LTF 对应的所有 HTF 层级的缓存指标。

    参数
    ----
    symbol: 交易品种
    ltf_timeframe: 当前策略运行的时间框架（如 "M5"）
    required: 可选，按 HTF 筛选所需指标名
              如 {"H1": ("rsi14", "adx14"), "H4": ("ema50",)}

    返回
    ----
    {htf_timeframe: {indicator_name: {field: value}}}
    例如：{"H1": {"rsi14": {"rsi": 45.2, "rsi_d3": -2.1}}}
    过期或不存在的层级不包含在返回值中。
    """
    result: Dict[str, Dict[str, Dict[str, float]]] = {}
    current = ltf_timeframe
    while True:
        htf = self._htf_map.get(current)
        if htf is None:
            break
        entry = self._get_valid_entry(symbol, htf)
        if entry is not None:
            if required and htf in required:
                filtered = {
                    k: v for k, v in entry.indicators.items()
                    if k in required[htf]
                }
            else:
                filtered = entry.indicators
            if filtered:
                result[htf] = filtered
        current = htf  # 继续向上查找更高层级
    return result
```

---

## 策略声明方式

### 新增可选属性：`htf_indicators`

```python
class SignalStrategy(Protocol):
    name: str
    required_indicators: tuple[str, ...]       # 当前 TF 指标（不变）
    preferred_scopes: tuple[str, ...]           # 不变
    regime_affinity: Dict[RegimeType, float]    # 不变

    # 新增（可选）—— 需要哪些 HTF 的哪些指标
    htf_indicators: Dict[str, tuple[str, ...]]  # {相对层级: (指标名,...)}
```

### 层级表示方式

用 `"+1"` / `"+2"` 表示相对于当前 TF 的高几级，**不硬编码绝对 TF**：

```python
class MyStrategy:
    name = "trend_htf_confirm"
    required_indicators = ("rsi14", "boll20")       # 当前 TF
    htf_indicators = {
        "+1": ("adx14", "ema50"),     # 高一级（M5→M15, H1→H4）
        "+2": ("ema50",),             # 高两级（M5→H1, H1→D1）
    }
    preferred_scopes = ("confirmed",)
    regime_affinity = { ... }
```

**为什么用相对层级而非绝对 TF**：
- 策略可复用于任何 TF（如同一策略在 M5 和 H1 上运行）
- 映射关系由 `HTFIndicatorCache._htf_map` 统一管理
- 配置在 `signal.ini` 的 `[htf_cache]` section 可调

### 向后兼容

- `htf_indicators` 是可选属性，默认空 `{}`
- 不声明的策略行为完全不变
- 现有 `get_htf_direction()` 保留（方向缓存独立于指标缓存）

---

## 自动推导 HTF eligible 指标

与 intrabar 指标推导完全一致的模式：

```python
# SignalModule 新增方法
def htf_required_indicators(self) -> Dict[str, frozenset[str]]:
    """从策略的 htf_indicators 自动推导哪些 HTF 指标需要缓存。

    返回 {绝对TF: frozenset(指标名)} 的映射。
    需要结合 app.ini 中配置的 timeframes 和 htf_map 展开。
    """
    # 遍历所有策略的 htf_indicators
    # 结合运行的 (symbol, tf) 列表展开相对层级为绝对 TF
    # 返回每个 TF 需要缓存的指标并集
```

### 推导流程（启动时一次性）

```
策略注册完成
  → SignalModule.htf_required_indicators()
    遍历所有策略 htf_indicators
    结合 app.ini 配置的 timeframes（如 M1, M5, H1）
    展开相对层级：
      M5 策略 "+1" → M15
      M5 策略 "+2" → H1
      H1 策略 "+1" → H4
      H1 策略 "+2" → D1
    返回 {"M15": {"adx14", "ema50"}, "H1": {"ema50"}, "H4": {"adx14", "ema50"}, ...}
  → 注入 HTFIndicatorCache.set_eligible_indicators(mapping)
  → 缓存只存储这些指标，其余丢弃
```

---

## Runtime 注入流程

### `_evaluate_strategies()` 中的变更

```python
# 在构建 regime_metadata 后、调用 service.evaluate() 前：
if self._htf_indicator_cache is not None:
    htf_data = self._htf_indicator_cache.get_htf_indicators(
        symbol, timeframe,
        required=strategy_htf_requirements.get(strategy),
    )
    if htf_data:
        regime_metadata["htf_indicators"] = htf_data
```

### 策略端使用

```python
class TrendHTFConfirmStrategy:
    htf_indicators = {"+1": ("adx14", "ema50")}

    def evaluate(self, context: SignalContext) -> SignalDecision:
        # 当前 TF 指标
        rsi = context.indicators.get("rsi14", {}).get("rsi")

        # HTF 指标（通过 metadata 访问）
        htf = context.metadata.get("htf_indicators", {})
        htf_1 = htf.get("+1", {})  # runtime 负责将绝对 TF 映射回相对层级
        htf_adx = htf_1.get("adx14", {}).get("adx")
        htf_ema = htf_1.get("ema50", {}).get("ema")

        if htf_adx is not None and htf_adx > 25:
            # H4 趋势强，提高当前信号置信度
            ...
```

---

## Intrabar 引用 Confirmed 指标

同样通过 `HTFIndicatorCache` 解决——把"上一次 confirmed 的当前 TF 快照"视为一种特殊的缓存层级。

### 策略声明

```python
class RsiWithEmaFilter:
    htf_indicators = {
        "+0": ("ema50", "sma20"),  # "+0" = 当前 TF 的上一次 confirmed 快照
        "+1": ("adx14",),          # 高一级 TF
    }
    preferred_scopes = ("intrabar", "confirmed")
```

`"+0"` 的语义：**不是更高时间框架，而是当前 TF 的最近一次 confirmed 快照**。
因为 confirmed 快照本来就会被缓存（对所有 TF），intrabar 评估时自然能从缓存中取到。

### 运行时行为

```
H1 confirmed 快照到达
  → HTFIndicatorCache.on_confirmed_snapshot("XAUUSD", "H1", ..., indicators)
  → 缓存 ("XAUUSD", "H1") 的 ema50, sma20

H1 intrabar 快照到达（只有 rsi14 等 8 个指标）
  → _evaluate_strategies() 评估 RsiWithEmaFilter
  → get_htf_indicators("XAUUSD", "H1")
    → "+0" → 从缓存取 ("XAUUSD", "H1") 的 ema50, sma20
    → "+1" → 从缓存取 ("XAUUSD", "H4") 的 adx14
  → 注入 metadata["htf_indicators"] = {"+0": {ema50: ...}, "+1": {adx14: ...}}
```

---

## TTL 策略

| 层级 | 默认 TTL | 说明 |
|------|---------|------|
| `+0`（同 TF confirmed） | 该 TF 的 bar 时长 × 2 | M5 → 10min, H1 → 2h, H4 → 8h |
| `+1` | HTF bar 时长 × 2 | H1→H4: 8h, M5→M15: 30min |
| `+2` | HTF bar 时长 × 2 | 同上逻辑 |

过期的缓存条目返回空 dict，策略需要处理指标缺失（与现有 required_indicators 缺失处理一致）。

TTL 通过 `signal.ini` 的 `[htf_cache]` 配置：
```ini
[htf_cache]
max_age_seconds = 14400       ; 信号方向缓存（现有）
indicator_ttl_multiplier = 2  ; 指标 TTL = bar时长 × 此值
```

---

## 与现有 HTFStateCache 的关系

### 方案：合并升级

将 `HTFIndicatorCache` 作为 `HTFStateCache` 的**超集**：

```python
class HTFIndicatorCache:
    """统一的 HTF 缓存，同时管理信号方向和指标快照。"""

    # === 原 HTFStateCache 功能（保留） ===
    def get_htf_direction(self, symbol, ltf_timeframe) -> Optional[str]
    def on_signal_event(self, event) -> None

    # === 新增：指标缓存 ===
    def on_confirmed_snapshot(self, symbol, tf, bar_time, indicators, scope) -> None
    def get_htf_indicators(self, symbol, ltf_tf, required=None) -> Dict
    def set_eligible_indicators(self, mapping: Dict[str, frozenset[str]]) -> None
```

**优势**：
- 单一组件，两种缓存共享 htf_map 和 TTL 配置
- 对外接口向后兼容（`get_htf_direction` 不变）
- 减少 listener 注册数量

---

## 完整数据流

```
IndicatorManager.publish_snapshot(scope="confirmed")
  ↓
HTFIndicatorCache.on_confirmed_snapshot()
  → 缓存 (symbol, tf) 的 eligible 指标

SignalRuntime.on_signal_event(confirmed_buy/sell)
  ↓
HTFIndicatorCache.on_signal_event()
  → 缓存 (symbol, tf) 的信号方向（现有逻辑）

SignalRuntime._evaluate_strategies()
  ↓ 对每个策略：
  ├─ 检查 strategy.htf_indicators
  ├─ 若非空：htf_cache.get_htf_indicators(symbol, tf)
  ├─ 将结果按相对层级键注入 metadata["htf_indicators"]
  └─ strategy.evaluate(context) → 策略通过 metadata 访问 HTF 指标
```

---

## 配置项汇总

### signal.ini

```ini
[htf_cache]
max_age_seconds = 14400          ; 信号方向 TTL（秒，现有）
indicator_ttl_multiplier = 2     ; 指标 TTL = bar时长 × 此值
enabled = true                   ; 全局开关

; 自定义 LTF→HTF 映射（可选覆盖默认）
; htf_map = M1:M5,M5:M15,M15:H1,H1:H4,H4:D1
```

### 策略声明（唯一配置入口）

```python
class MyStrategy:
    htf_indicators = {
        "+0": ("ema50",),        # 同 TF confirmed 快照
        "+1": ("adx14", "rsi14"), # 高一级 TF
        "+2": ("ema50",),        # 高两级 TF
    }
```

不声明 = 不使用 HTF 指标 = 系统不缓存该策略的 HTF 数据（需求驱动）。

---

## 实现步骤

1. **扩展 `HTFStateCache` → `HTFIndicatorCache`**
   - 新增 `_indicator_cache`、`_eligible_indicators`
   - 新增 `on_confirmed_snapshot()`、`get_htf_indicators()`、`set_eligible_indicators()`
   - 保留所有现有方法（向后兼容）

2. **策略 Protocol 新增可选属性**
   - `base.py` 的 `SignalStrategy` Protocol 添加 `htf_indicators`（默认 `{}`）
   - `_validate_strategy_attrs()` 不强制要求此属性

3. **SignalModule 新增推导方法**
   - `htf_required_indicators()` → 从策略声明推导需要缓存的指标

4. **工厂函数注册 snapshot listener**
   - `build_signal_components()` 中将 `HTFIndicatorCache` 注册为 IndicatorManager 的 snapshot_listener
   - 调用 `set_eligible_indicators()` 注入推导结果

5. **Runtime 评估时注入**
   - `_evaluate_strategies()` 中查询 HTF 指标并注入 metadata

6. **更新 CLAUDE.md**
   - 新增 HTF 指标缓存相关文档
   - 更新策略 SOP 中的属性说明
