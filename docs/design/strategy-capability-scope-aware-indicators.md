# StrategyCapability scope-aware needed_indicators 设计

**Status**: Draft (2026-04-28)
**Owner**: hugo-lwh@hotmail.com
**Trigger**: demo-main 04-27/04-28 反复报 `Strategy XAUUSD/H1/structured_pullback_window skipped: missing indicators ['ema55']`

## 1. 背景

`StrategyCapability.needed_indicators` 当前是单一字段，confirmed 与 intrabar 两个 scope 共享。但实际上：

- **confirmed 路径**：`set_confirmed_eligible_override` 收 `confirmed_required_indicators() | htf_required_indicators() | _INFRA_INDICATORS`，集合大、计算路径稳定（完整 closed bars + 最大 lookback）
- **intrabar 路径**：`set_intrabar_eligible_override` 收 `intrabar_required_indicators()`（仅 `needs_intrabar=True` 策略的并集），合成 partial bar，每次 M5 close 重算

ema55 这种依赖**长 history 的稳态指标**在 intrabar 路径下：
- partial bar 算 ema55 时，"当前 close" 是临时值，partial 推进过程中 ema55 反复抖动
- `select_indicator_names_for_history` 在 prefix 长度 < 55 的边界条件下 silent drop ema55
- evaluator 收到 dict 后报 missing → skip 评估

策略 `pullback_window` 在 intrabar 上**不应该期望** ema55 可用——它是趋势均线，partial bar 上 ema55 抖动没有决策价值。但当前 capability 让 evaluator 错误地把 confirmed 与 intrabar 混在同一个 needed_indicators 中校验。

## 2. 目标

让 `StrategyCapability` 显式区分 confirmed scope 与 intrabar scope 的 needed_indicators，evaluator 按 scope 选对应集合校验。根因修复，避免：
- silent skip indicator 子集 → evaluator 报 missing 误警
- 策略错误期望 intrabar 上有不稳定指标
- 未来新策略撞同样问题

## 3. 非目标

- 不改变 indicator 计算路径（不动 `process_intrabar_event` 的 eligible 选择）
- 不改变 evaluator 的 trigger 时序（不引入异步重试）
- 不改变 confirmed scope 的现有行为
- 不修复 intrabar partial bar 上 ema55 抖动本身——只是不让策略依赖它

## 4. 当前实现

### 4.1 capability 契约
[`src/signals/contracts/capability.py:8-33`](../../src/signals/contracts/capability.py#L8-L33)

```python
@dataclass(frozen=True)
class StrategyCapability:
    name: str
    valid_scopes: tuple[str, ...]
    needed_indicators: tuple[str, ...]   # ← 单一字段
    needs_intrabar: bool
    needs_htf: bool
    regime_affinity: dict[str, float]
    htf_requirements: dict[str, str]
```

### 4.2 capability 构造
[`src/signals/service.py:305-335`](../../src/signals/service.py#L305-L335)

```python
needed_indicators = tuple(str(item) for item in impl.required_indicators)
valid_scopes = tuple(str(scope) for scope in impl.preferred_scopes)
capabilities.append(
    StrategyCapability(
        ...
        needed_indicators=needed_indicators,
        needs_intrabar=("intrabar" in valid_scopes),
        ...
    )
)
```

策略类只有一个 `required_indicators` class attr，confirmed/intrabar 共用。

### 4.3 evaluator 校验
[`src/signals/orchestration/runtime_evaluator.py:56-61`](../../src/signals/orchestration/runtime_evaluator.py#L56-L61)

```python
required_indicators = capability.needed_indicators   # ← scope 无关
if required_indicators:
    missing = [ind for ind in required_indicators if ind not in indicators]
    if missing:
        _record_indicator_miss(...)
        continue
```

### 4.4 intrabar required indicators
[`src/signals/service.py:205-213`](../../src/signals/service.py#L205-L213)

```python
def intrabar_required_indicators(self) -> frozenset:
    result: set[str] = set()
    for capability in self.strategy_capability_catalog():
        if not capability.needs_intrabar:
            continue
        for ind in capability.needed_indicators:   # ← 全集
            result.add(str(ind))
    return frozenset(result)
```

## 5. 新设计

### 5.1 capability 加 scope-specific 字段

```python
@dataclass(frozen=True)
class StrategyCapability:
    name: str
    valid_scopes: tuple[str, ...]
    needed_indicators: tuple[str, ...]              # 保留作 confirmed 默认 + 向后兼容
    intrabar_needed_indicators: tuple[str, ...] = ()  # 新增：intrabar scope 子集
    needs_intrabar: bool
    needs_htf: bool
    regime_affinity: dict[str, float]
    htf_requirements: dict[str, str]
```

约束：
- `intrabar_needed_indicators ⊆ needed_indicators`（intrabar 子集必须是 confirmed 全集的子集；启动期校验）
- `needs_intrabar=False` 时 `intrabar_needed_indicators` 必须为空（即不允许声明 intrabar 集但又不开启 intrabar；fail-fast）
- 留空（默认）= confirmed 与 intrabar 共用同集合（向后兼容现有 14 个策略；只有需要区分的策略需主动覆盖）

### 5.2 策略类扩展声明

策略基类 `StructuredStrategyBase` 加 class attr：
```python
class StructuredStrategyBase:
    required_indicators: ClassVar[tuple[str, ...]] = ()
    intrabar_required_indicators: ClassVar[tuple[str, ...] | None] = None
    # None = 默认 confirmed 与 intrabar 共用 required_indicators（向后兼容）
    # tuple = 显式声明 intrabar scope 上需要的子集
```

`pullback_window` 改：
```python
class StructuredPullbackWindow(StructuredStrategyBase):
    required_indicators = (
        "ema9", "ema21", "ema55", "rsi14", "atr14", "adx14", "boll20",
    )
    intrabar_required_indicators = (
        "ema9", "ema21", "rsi14", "atr14",
        # 去掉 ema55 / adx14 / boll20 — 这些在 partial bar 上不稳定
        # 评估时只用短均线 ema9/ema21 + rsi14 触发器 + atr14 距离
    )
```

其他 13 个策略类**默认不声明** `intrabar_required_indicators`（保持现有行为）。

### 5.3 capability 构造改造

```python
needed_indicators = tuple(str(item) for item in impl.required_indicators)
intrabar_subset = getattr(impl, "intrabar_required_indicators", None)
if intrabar_subset is None:
    intrabar_needed = needed_indicators  # 默认与 confirmed 同
else:
    intrabar_needed = tuple(str(item) for item in intrabar_subset)

capabilities.append(
    StrategyCapability(
        ...
        needed_indicators=needed_indicators,
        intrabar_needed_indicators=intrabar_needed,
        ...
    )
)
```

### 5.4 evaluator scope-aware 校验

```python
def _select_needed_indicators(capability, scope):
    """根据评估 scope 返回该策略所需 indicators 集合。"""
    if scope == "intrabar":
        return capability.intrabar_needed_indicators
    return capability.needed_indicators

# evaluate_strategies 内：
required_indicators = _select_needed_indicators(capability, scope)
if required_indicators:
    missing = [ind for ind in required_indicators if ind not in indicators]
    ...
```

### 5.5 service.intrabar_required_indicators 改用新字段

```python
def intrabar_required_indicators(self) -> frozenset:
    result: set[str] = set()
    for capability in self.strategy_capability_catalog():
        if not capability.needs_intrabar:
            continue
        for ind in capability.intrabar_needed_indicators:   # ← 子集
            result.add(str(ind))
    return frozenset(result)
```

含意：indicator manager 的 `set_intrabar_eligible_override` 收到的集合**变小**（只含 intrabar 真用的）。intrabar 路径计算成本下降（少算几个 indicator）。

### 5.6 启动期校验

`validate_strategy_deployment_contracts` 之前/之后加：
```python
def validate_intrabar_subset_constraint(catalog):
    for cap in catalog:
        confirmed_set = set(cap.needed_indicators)
        intrabar_set = set(cap.intrabar_needed_indicators)
        if not intrabar_set.issubset(confirmed_set):
            extras = intrabar_set - confirmed_set
            raise ValueError(
                f"strategy {cap.name}: intrabar_required_indicators "
                f"{sorted(extras)} not in required_indicators"
            )
        if not cap.needs_intrabar and intrabar_set:
            raise ValueError(
                f"strategy {cap.name}: needs_intrabar=False but declares "
                f"intrabar_required_indicators={sorted(intrabar_set)}"
            )
```

## 6. 影响面

### 6.1 文件改动清单

| 文件 | 改动 |
|---|---|
| `src/signals/contracts/capability.py` | 加 `intrabar_needed_indicators` 字段 + `from_contract` 反序列化 + `as_contract` 序列化 |
| `src/signals/strategies/structured/base.py` | 基类 class attr `intrabar_required_indicators: ClassVar[...] = None` |
| `src/signals/strategies/structured/pullback_window.py` | 显式声明 `intrabar_required_indicators` 子集 |
| `src/signals/service.py` | capability 构造读 `intrabar_required_indicators` + `intrabar_required_indicators()` 改用新字段 |
| `src/signals/orchestration/runtime_evaluator.py` | `_select_needed_indicators(capability, scope)` |
| `src/signals/orchestration/policy.py` | `needed_indicators_for(strategy, scope)` 公开 port |
| `src/signals/contracts/deployment.py` 或新文件 | `validate_intrabar_subset_constraint(catalog)` |
| `src/app_runtime/factories/signals.py` | 校验调用挂入 `_validate_strategy_deployment_contracts` 之后 |
| `tests/signals/test_capability.py` | 4-5 个新单测：默认行为 / 显式子集 / 子集越界 raise / needs_intrabar=False 不允许声明 |
| `tests/signals/test_runtime_evaluator.py` | 1-2 个新单测：confirmed 与 intrabar 走不同集合 |

### 6.2 向后兼容

- 现有 13 个策略不声明 `intrabar_required_indicators` → capability 取默认（与 `needed_indicators` 同）→ 行为不变
- 仅 `pullback_window` 显式声明 intrabar 子集 → 该策略 intrabar 评估时只校验子集，不再误报 ema55 missing
- capability 序列化新增字段，反序列化默认值兼容旧 contract

### 6.3 性能

- intrabar 路径计算成本下降（intrabar_eligible 集合变小）
- evaluator 检查 missing 集合变小（O(集合大小)）—— 微小正向

### 6.4 ADR 候选

本次改动**触及 SignalCapability 契约**，应升级为 ADR-013（或下个编号）。spec 完成后 `docs/design/adr.md` 追加。

## 7. 测试策略

### 7.1 单元测试

1. **默认行为**：策略不声明 `intrabar_required_indicators` → capability.intrabar_needed_indicators == needed_indicators
2. **显式子集**：策略声明子集 → capability.intrabar_needed_indicators 仅含声明项
3. **子集越界 fail-fast**：策略声明 intrabar 集含 needed 之外的 indicator → 启动 raise ValueError
4. **needs_intrabar=False 不允许声明**：策略 valid_scopes=["confirmed"] 但声明 intrabar 集 → raise
5. **evaluator scope-aware**：同一策略 confirmed 评估校验全集，intrabar 评估校验子集

### 7.2 回归

- `tests/signals/` 全量
- `tests/app_runtime/` 全量
- demo-main 重启后跑 24h 观察 H1 整点不再报 ema55 missing

### 7.3 验收标准

- ✅ pullback_window intrabar 评估在 H1 confirmed close 时不再 skip
- ✅ pullback_window confirmed 评估行为不变（仍校验 ema55）
- ✅ 其他 13 个策略行为完全不变
- ✅ 新策略若误声明非法 intrabar 集 → 启动 fail-fast

## 8. 风险与权衡

### 8.1 风险

- **R1**：capability 序列化新增字段——所有 reconciliation 路径（runtime_components / 测试 stub）都要兼容默认值
- **R2**：策略基类加 ClassVar 后 mypy strict 要求所有子类要么继承默认值要么显式赋值——14 个策略类都要 lint 一遍
- **R3**：发现还有别的策略也应该有 intrabar 子集（比如 trend_continuation 的 supertrend14）——本次只改 pullback_window，其他策略保持默认；后续若发现再补

### 8.2 权衡

vs 方案 B（intrabar 也算全集）：
- B 改动 1 行代码（一行 fn 调用变成同名）
- B intrabar 计算成本上升（每次 M5 close 多算几个 indicator）
- C 治本但工作量 ~200 行

按 demo 实测：M5 close 触发 H1 intrabar 重算频率为每 5min 一次，每次多算 ~5 个 indicator 成本可承受。但 C 让契约自洽（策略明确表达 scope 差异），治本意义大。

## 9. 实施顺序

按 Plan 的顺序：

1. capability 契约扩展（加字段、from/as_contract、默认值）+ 单测
2. 启动期校验函数 + 单测（子集约束 + needs_intrabar 一致性）
3. service 构造逻辑 + 单测
4. evaluator scope-aware + 单测
5. policy 公开 port
6. 策略基类 ClassVar 默认值
7. pullback_window 显式声明 intrabar 子集
8. factories/signals 校验挂入
9. 全量回归（signals + app_runtime + 重启 demo 验证）
10. ADR-013 追加 + codebase-review 记录

每步独立可测，可分开 commit。

## 10. 未决项

- pullback_window 在 intrabar 上的 `intrabar_required_indicators` 应该具体含哪些？本 spec 提议 `("ema9", "ema21", "rsi14", "atr14")` 但需评审——是否完全去掉 ema55？还是保留但改用更短均线（如 ema21 替代 ema55 的角色）？该问题超出本 spec 范围，留给后续策略调优 PR。
