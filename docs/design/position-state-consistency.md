# 持仓状态一致性优化设计

> 状态：**IMPLEMENTED**  
> 创建：2026-04-16  
> 关联：`src/trading/positions/manager.py`, `reconciliation.py`, `src/trading/state/store.py`

---

## 1. 问题诊断

当前持仓管理存在**内存 / DB / MT5 Broker 三方状态漂移**问题，核心风险集中在：

### 1.1 peak_price 丢失窗口（P0）

**现状**：`update_price()` 实时更新内存中的 `highest_price / lowest_price`，但 DB 持久化依赖 reconcile 周期（默认 10s）。进程崩溃时**最多丢失 9.9s 的价格历史**。

**后果**：重启后 Chandelier Exit 的 trailing stop 基于过时的 peak_price 计算——
- buy 持仓 peak 从 2060 回退到 2055（10s 前的 reconcile 值），trailing SL 被错误收紧
- breakeven 状态丢失：已激活但未 flush 到 DB 的 `breakeven_activated` 回退为 False

**代码位置**：
- `manager.py:304-325` — `update_price()` 锁内更新 + 锁外 MT5 API
- `manager.py:525-538` — `_reconcile_loop()` 10s 周期
- `store.py:303-304` — `record_position_update()` 仅在 reconcile 时被调用

### 1.2 initial_stop_loss 恢复降级（P0）

**现状**：`reconciliation.py:213-224` 恢复持仓时，如果 DB 中 `initial_stop_loss` 缺失或为 0，fallback 到当前 SL。

**后果**：entry=2000, 初始 SL=1980(R=20), trail 到 SL=1995 后重启 → R 被错误计算为 5，breakeven 阈值和 lock_ratio 全部偏移。

### 1.3 双重 resolver 无冲突检测（P1）

**现状**：`reconciliation.py:100-125` 先查 `position_state_resolver`（DB），再查 `position_context_resolver`（comment 解码），两个 dict 简单 merge（后者覆盖前者），无冲突检测。

**后果**：两个源的 `entry_price` 或 `atr_at_entry` 不一致时，结果不确定。

---

## 2. 设计方案

### 2.1 脏标记 + 增量 flush（解决 peak_price 丢失）

**核心思路**：在 `update_price()` 中标记"脏"持仓，由独立的轻量 flush 机制以更短的间隔持久化关键字段，而不是依赖重量级的 reconcile 周期。

#### 2.1.1 TrackedPosition 新增版本字段

```python
# manager.py — TrackedPosition dataclass 新增
@dataclass
class TrackedPosition:
    # ... 现有字段 ...
    
    # 状态版本控制（不参与外部序列化，仅运行时用）
    _dirty: bool = field(default=False, repr=False)
    _version: int = field(default=0, repr=False)       # 每次关键字段修改递增
    _flushed_version: int = field(default=0, repr=False)  # 最后 flush 到 DB 的版本
```

#### 2.1.2 update_price 标记脏位

```python
def update_price(self, ticket: int, current_price: float) -> None:
    with self._lock:
        pos = self._positions.get(ticket)
        if pos is None:
            return
        pos.current_price = current_price
        changed = False
        if pos.action == "buy":
            if pos.highest_price is None or current_price > pos.highest_price:
                pos.highest_price = current_price
                pos.peak_price = pos.highest_price
                changed = True
        elif pos.action == "sell":
            if pos.lowest_price is None or current_price < pos.lowest_price:
                pos.lowest_price = current_price
                pos.peak_price = pos.lowest_price
                changed = True
        if changed:
            pos._version += 1
            pos._dirty = True
        
        pending_action = self._evaluate_chandelier_exit(pos, current_price)
    
    if pending_action is not None:
        self._apply_chandelier_action(pending_action)
```

#### 2.1.3 增量 flush 机制

在 `_reconcile_loop` 中增加轻量 flush 步骤，**不替换现有 reconcile，而是在每个周期开始前先 flush 脏位**：

```python
def _reconcile_loop(self) -> None:
    while not self._stop_event.is_set():
        try:
            self._flush_dirty_positions()    # 新增：先 flush 脏位
            self._run_end_of_day_closeout()
            self._reconcile_with_mt5()
            self._check_regime_changes()
            self._run_margin_guard()
            self._reconcile_count += 1
            self._last_reconcile_at = datetime.now(timezone.utc)
            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)
            logger.warning("PositionManager reconcile error: %s", exc)
        self._stop_event.wait(timeout=self._reconcile_interval)

def _flush_dirty_positions(self) -> None:
    """增量 flush：仅持久化 _dirty 标记的持仓的关键字段。"""
    if self._on_position_updated is None:
        return
    with self._lock:
        dirty_snapshot = [
            pos for pos in self._positions.values()
            if pos._dirty and pos._version > pos._flushed_version
        ]
    for pos in dirty_snapshot:
        try:
            self._on_position_updated(pos, "dirty_flush")
            with self._lock:
                pos._flushed_version = pos._version
                pos._dirty = False
        except Exception as exc:
            logger.debug(
                "PositionManager: dirty flush ticket=%d failed: %s",
                pos.ticket, exc,
            )
```

**收益**：
- reconcile 间隔仍保持 10s（重量级：MT5 API 调用 + 全量对账）
- 脏位 flush 在每个 reconcile 周期开头执行，延迟从 0~10s 降低到 0~10s 内的第一个周期
- 如需进一步降低，可将 `_flush_dirty_positions` 拆成独立的 2-3s 间隔小循环

**进阶方案**（可选，当前规模不需要）：
- 独立 flush 线程 + 2s 间隔
- 只 flush `highest_price / lowest_price / breakeven_activated / trailing_active` 四个字段（轻量 UPDATE）

### 2.2 initial_stop_loss 校验与保护

在恢复逻辑中添加一致性校验，**拒绝不合理的 fallback**：

```python
# reconciliation.py — sync_open_positions 中 initial_stop_loss 恢复部分

# 新增：校验恢复的 initial_stop_loss 合理性
if effective_initial_sl > 0:
    pos.initial_stop_loss = float(effective_initial_sl)
    pos.initial_risk = abs(pos.entry_price - pos.initial_stop_loss)
    
    # 校验：initial_risk 不能太小（< 0.1 ATR 视为异常）
    if pos.atr_at_entry > 0 and pos.initial_risk < 0.1 * pos.atr_at_entry:
        logger.warning(
            "Position ticket=%d recovered with suspiciously small "
            "initial_risk=%.4f (< 0.1 ATR=%.4f). "
            "Possible fallback to trailed SL. Using ATR-based estimate.",
            ticket,
            pos.initial_risk,
            pos.atr_at_entry,
        )
        # 使用 ATR 倍数估算原始 SL 距离
        estimated_risk = pos.atr_at_entry * max(pos.sl_atr_mult, 1.5)
        pos.initial_risk = estimated_risk
        if pos.action == "buy":
            pos.initial_stop_loss = pos.entry_price - estimated_risk
        else:
            pos.initial_stop_loss = pos.entry_price + estimated_risk
```

### 2.3 Resolver 冲突检测

合并双 resolver 为单一 resolver，消除覆盖歧义：

```python
# reconciliation.py — 替换当前的双 resolver merge 逻辑

def _resolve_position_context(
    manager: PositionManager,
    ticket: int,
    comment: str,
) -> dict[str, Any]:
    """从 DB state + comment 解码合并持仓恢复上下文，冲突时 DB 优先。"""
    db_state = {}
    if manager._position_state_resolver is not None:
        try:
            db_state = dict(manager._position_state_resolver(ticket) or {})
        except Exception:
            logger.debug("Position state resolver failed for ticket=%s", ticket, exc_info=True)

    comment_context = {}
    if manager._position_context_resolver is not None:
        try:
            comment_context = dict(manager._position_context_resolver(ticket, comment) or {})
        except Exception:
            logger.debug("Position context resolver failed for ticket=%s", ticket, exc_info=True)

    # 合并策略：DB 是权威源，comment 仅填补 DB 中缺失的字段
    merged = dict(comment_context)  # comment 作为底层
    merged.update({k: v for k, v in db_state.items() if v is not None})  # DB 覆盖非 None 字段

    # 冲突检测：关键字段不一致时记录 WARNING
    for key in ("entry_price", "initial_stop_loss", "atr_at_entry"):
        db_val = db_state.get(key)
        ctx_val = comment_context.get(key)
        if db_val is not None and ctx_val is not None:
            try:
                if abs(float(db_val) - float(ctx_val)) / max(abs(float(db_val)), 1e-9) > 0.01:
                    logger.warning(
                        "Position ticket=%d resolver conflict on '%s': "
                        "db=%.5f vs comment=%.5f — using DB value",
                        ticket, key, float(db_val), float(ctx_val),
                    )
            except (TypeError, ValueError):
                pass

    return merged
```

---

## 3. 实施计划

| 阶段 | 内容 | 风险 | 改动量 |
|------|------|------|--------|
| Phase 1 | 脏标记 + `_flush_dirty_positions()` | 低——纯追加，不改现有 reconcile | ~50 行 |
| Phase 2 | `initial_stop_loss` 校验 + ATR 估算 fallback | 低——仅在恢复时生效 | ~20 行 |
| Phase 3 | Resolver 冲突检测 + DB 优先合并策略 | 低——仅改 merge 顺序 + 加日志 | ~40 行 |

---

## 4. 不做什么

- **不引入独立 flush 线程**：当前持仓数 < 10，在 reconcile 循环中顺带 flush 足够
- **不做 WAL 级持久化**：peak_price 丢失影响的是 trail 精度而非交易正确性（fail-safe：trail 不够紧 → 持仓保留更久，不会错误平仓）
- **不改 TrackedPosition 序列化格式**：`_dirty/_version/_flushed_version` 用下划线前缀 + `repr=False`，不影响外部接口
