# Economic Decay Service 模块化重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `_query_symbol_event_decay` 触发的 P1 静默失效（缺 `timezone` import + `except Exception` 吞 NameError + cache 1.0 缓存 60s）连同其根因一并整改：抽 `EconomicDecayService` 作为 calendar 域唯一对外端口，统一处理"经济事件 decay 因子查询"，强制 `at_time` 显式注入，异常三段分层（设计降级 / 运行时降级 + WARNING / 编码错误透传），signal/risk/backtest 各域只调端口；同时去除 signal evaluator 与 filters 之间的复制粘贴漂移与跨域 import。

**Architecture:** 端口抽离 + 时间显式注入 + 异常分层契约。新建 `src/calendar/economic_decay.py:EconomicDecayService` 持有 provider/policy/cache 全部内部细节；`signal evaluator` 与 `EconomicEventFilter` 都委托同一实例；`infer_symbol_context` 上提为 `src/calendar/__init__.py` 公共出口；`apply_confidence_adjustments` 改为接收 `event_time` 并传入 `decay_factor(symbol, event_time)`，废除 wall clock 用法；测试架构改为注入 stub service，禁止 monkeypatch 桩本体。

**Tech Stack:** Python 3.9+ / Pydantic v2 / pytest / mypy strict / dataclass / Protocol

---

## File Structure

| 文件 | 角色 | 动作 |
|---|---|---|
| `src/calendar/economic_decay.py` | 新建 service 端口 | Create |
| `src/calendar/__init__.py` | 导出 `EconomicDecayService` + `infer_symbol_context` | Modify |
| `src/signals/orchestration/runtime_evaluator.py` | 删除 4 个内部函数 + 改调用 service + `apply_confidence_adjustments` 接 `event_time` 参数 + `_resolve_event_impact_forecast` 同步异常分层 | Modify |
| `src/signals/orchestration/runtime.py` | 删 `_event_decay_cache` 字段 + 加 `economic_decay_service` setter/property + 调用链传 `event_time` | Modify |
| `src/signals/execution/filters.py` | `EconomicEventFilter` 内部委托 service + 删 `_SIGNAL_EVENT_STATUSES` 与 `_symbol_context` 重复 | Modify |
| `src/app_runtime/container.py` | `__slots__` 与 `__init__` 新增 `economic_decay_service` 字段 | Modify |
| `src/app_runtime/factories/signals.py` | 新增 `build_economic_decay_service` + 把同一 service 注入 filter 与 runtime + 热重载路径同步重建 | Modify |
| `src/app_runtime/builder_phases/signal.py` | 装配 service 到 container | Modify |
| `tests/calendar/test_economic_decay.py` | service 单元测试（timezone 锁死、at_time 注入、cache、降级、fail-fast） | Create |
| `tests/signals/test_runtime_evaluator.py` | 删除 monkeypatch 桩本体；改注入 stub service | Modify |
| `tests/signals/execution/test_filters.py` | 同步 EconomicEventFilter 委托后的契约测试 | Modify (or Create if absent) |
| `docs/design/adr.md` | 新增 ADR-011 | Modify |
| `docs/codebase-review.md` | 新增 §0g 状态台账 | Modify |
| `CLAUDE.md` | 更新模块路径表 + Known Issues | Modify |

---

## Task 1: 新建 EconomicDecayService 单元测试（先写失败测试，锁死 P1 不回归）

**Files:**
- Create: `tests/calendar/test_economic_decay.py`

- [ ] **Step 1.1: 创建测试文件**

```python
# tests/calendar/test_economic_decay.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.calendar.economic_decay import EconomicDecayService
from src.calendar.policy import (
    EconomicEventWindow,
    SignalEconomicPolicy,
)


@dataclass
class _StubEvent:
    event_time: datetime


class _StubProvider:
    """Records last call args; configurable return + side_effect."""

    def __init__(
        self,
        events: list[Any] | None = None,
        side_effect: BaseException | None = None,
    ) -> None:
        self.events = events or []
        self.side_effect = side_effect
        self.calls: list[dict[str, Any]] = []

    def get_events(self, **kwargs: Any) -> list[Any]:
        self.calls.append(kwargs)
        if self.side_effect is not None:
            raise self.side_effect
        return list(self.events)


def _policy(
    *,
    enabled: bool = True,
    decay_pre: int = 30,
    hard_block_pre: int = 5,
    decay_post: int = 30,
    hard_block_post: int = 5,
    importance_min: int = 3,
) -> SignalEconomicPolicy:
    return SignalEconomicPolicy(
        enabled=enabled,
        filter_window=EconomicEventWindow(
            lookahead_minutes=hard_block_pre,
            lookback_minutes=hard_block_post,
            importance_min=importance_min,
        ),
        query_window=EconomicEventWindow(
            lookahead_minutes=decay_pre,
            lookback_minutes=decay_post,
            importance_min=importance_min,
        ),
        hard_block_pre_minutes=hard_block_pre,
        hard_block_post_minutes=hard_block_post,
        decay_pre_minutes=decay_pre,
        decay_post_minutes=decay_post,
    )


_AT_TIME = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)


def test_decay_factor_requires_explicit_at_time() -> None:
    """signature 强制 at_time 关键字，禁止隐式 datetime.now() 调用。"""
    service = EconomicDecayService(provider=_StubProvider(), policy=_policy())
    with pytest.raises(TypeError):
        service.decay_factor("XAUUSD")  # type: ignore[call-arg]


def test_returns_one_when_policy_disabled() -> None:
    service = EconomicDecayService(
        provider=_StubProvider(), policy=_policy(enabled=False)
    )
    assert service.decay_factor("XAUUSD", at_time=_AT_TIME) == 1.0


def test_returns_one_when_no_events() -> None:
    service = EconomicDecayService(provider=_StubProvider(events=[]), policy=_policy())
    assert service.decay_factor("XAUUSD", at_time=_AT_TIME) == 1.0


def test_query_window_uses_at_time_not_wall_clock() -> None:
    """关键回归：禁止再用 datetime.now()。查询窗口必须围绕传入的 at_time。"""
    provider = _StubProvider(events=[])
    service = EconomicDecayService(provider=provider, policy=_policy(decay_pre=30, decay_post=30))
    historical = datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc)
    service.decay_factor("XAUUSD", at_time=historical)
    call = provider.calls[-1]
    assert call["start_time"] == historical - timedelta(minutes=30)
    assert call["end_time"] == historical + timedelta(minutes=30)


def test_picks_closest_event_for_decay() -> None:
    near = _StubEvent(event_time=_AT_TIME + timedelta(minutes=10))
    far = _StubEvent(event_time=_AT_TIME + timedelta(minutes=25))
    service = EconomicDecayService(
        provider=_StubProvider(events=[far, near]),
        policy=_policy(decay_pre=30, hard_block_pre=5),
    )
    decay = service.decay_factor("XAUUSD", at_time=_AT_TIME)
    # delta = +10 min, in (hard_block_pre=5, decay_pre=30] → linear
    expected = max(0.0, min(1.0, (10 - 5) / (30 - 5)))
    assert decay == pytest.approx(expected)


def test_cache_hit_within_ttl() -> None:
    provider = _StubProvider(events=[])
    service = EconomicDecayService(
        provider=provider, policy=_policy(), cache_ttl_seconds=60.0
    )
    service.decay_factor("XAUUSD", at_time=_AT_TIME)
    service.decay_factor("XAUUSD", at_time=_AT_TIME + timedelta(seconds=30))
    assert len(provider.calls) == 1


def test_cache_expires_after_ttl() -> None:
    provider = _StubProvider(events=[])
    service = EconomicDecayService(
        provider=provider, policy=_policy(), cache_ttl_seconds=60.0
    )
    service.decay_factor("XAUUSD", at_time=_AT_TIME)
    service.decay_factor("XAUUSD", at_time=_AT_TIME + timedelta(seconds=120))
    assert len(provider.calls) == 2


def test_cache_capacity_evicts_oldest() -> None:
    provider = _StubProvider(events=[])
    service = EconomicDecayService(
        provider=provider, policy=_policy(), cache_max_entries=2
    )
    service.decay_factor("XAUUSD", at_time=_AT_TIME)
    service.decay_factor("EURUSD", at_time=_AT_TIME + timedelta(seconds=1))
    service.decay_factor("USDJPY", at_time=_AT_TIME + timedelta(seconds=2))
    # XAUUSD evicted; re-query triggers provider call again.
    service.decay_factor("XAUUSD", at_time=_AT_TIME + timedelta(seconds=3))
    assert len(provider.calls) == 4


def test_runtime_failure_returns_one_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    """ConnectionError 等真实降级场景：返回 1.0 + WARNING 日志。"""
    provider = _StubProvider(side_effect=ConnectionError("DB unreachable"))
    service = EconomicDecayService(provider=provider, policy=_policy())
    with caplog.at_level(logging.WARNING, logger="src.calendar.economic_decay"):
        decay = service.decay_factor("XAUUSD", at_time=_AT_TIME)
    assert decay == 1.0
    assert any("XAUUSD" in rec.message and "ConnectionError" in rec.message for rec in caplog.records)


def test_runtime_failure_does_not_cache() -> None:
    """降级返回 1.0 不应写入缓存（避免把 outage 永久固化）。"""
    provider = _StubProvider(side_effect=ConnectionError("DB"))
    service = EconomicDecayService(
        provider=provider, policy=_policy(), cache_ttl_seconds=60.0
    )
    service.decay_factor("XAUUSD", at_time=_AT_TIME)
    provider.side_effect = None
    provider.events = []
    service.decay_factor("XAUUSD", at_time=_AT_TIME + timedelta(seconds=10))
    assert len(provider.calls) == 2


def test_coding_error_propagates_fail_fast() -> None:
    """NameError/AttributeError/ImportError/TypeError 必须直接抛，禁止 except Exception 兜底。

    本测试反向锁死：未来若有人重新写 except Exception 把这类异常吞掉，
    本测试会立即红，阻止同类静默失效再发生。
    """
    provider = _StubProvider(side_effect=NameError("timezone is not defined"))
    service = EconomicDecayService(provider=provider, policy=_policy())
    with pytest.raises(NameError):
        service.decay_factor("XAUUSD", at_time=_AT_TIME)


def test_has_blocking_event_uses_filter_window() -> None:
    """硬阻断查询：使用 filter_window 而非 query_window。"""
    provider = _StubProvider(events=[_StubEvent(event_time=_AT_TIME + timedelta(minutes=2))])
    service = EconomicDecayService(
        provider=provider,
        policy=_policy(hard_block_pre=5, hard_block_post=5, decay_pre=30, decay_post=30),
    )
    blocked, reason = service.has_blocking_event("XAUUSD", at_time=_AT_TIME)
    assert blocked is True
    assert reason == "economic_event_block"
    call = provider.calls[-1]
    assert call["start_time"] == _AT_TIME - timedelta(minutes=5)
    assert call["end_time"] == _AT_TIME + timedelta(minutes=5)


def test_has_blocking_event_clear_when_no_events() -> None:
    provider = _StubProvider(events=[])
    service = EconomicDecayService(provider=provider, policy=_policy())
    blocked, reason = service.has_blocking_event("XAUUSD", at_time=_AT_TIME)
    assert blocked is False
    assert reason == ""
```

- [ ] **Step 1.2: 运行测试确认全红**

Run: `pytest tests/calendar/test_economic_decay.py -v`
Expected: 全部 FAIL，errors 包含 `ModuleNotFoundError: No module named 'src.calendar.economic_decay'`

- [ ] **Step 1.3: Commit 失败测试**

```bash
git add tests/calendar/test_economic_decay.py
git commit -m "test(calendar): add EconomicDecayService failing tests (P1 regression lock)

- timezone 回归测试锁死 datetime.now 误用
- fail-fast 反向测试锁死 except Exception 不可再退化
- cache/降级/at_time 注入完整契约覆盖"
```

---

## Task 2: 实现 EconomicDecayService 让测试通过

**Files:**
- Create: `src/calendar/economic_decay.py`

- [ ] **Step 2.1: 实现 service**

```python
# src/calendar/economic_decay.py
"""Calendar-domain economic event decay query port.

Single entry point for signal/risk/backtest domains to ask:
    "Given symbol S and evaluation time T, what's the confidence
     decay factor in [0.0, 1.0] caused by nearby high-impact events?"

This module owns:
- provider query parameter assembly
- symbol → countries/currencies inference
- per-symbol short-TTL cache (60s default)
- exception layering contract (see decay_factor docstring)

Callers MUST pass an explicit `at_time` so this service stays
deterministic under backtest/replay (no implicit wall-clock reads).
"""

from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, timedelta
from typing import Any, Optional, Protocol

from src.calendar.policy import SignalEconomicPolicy

logger = logging.getLogger(__name__)

_SIGNAL_EVENT_STATUSES = (
    "scheduled",
    "imminent",
    "pending_release",
    "released",
)


class EconomicEventsProvider(Protocol):
    def get_events(
        self,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int = 1000,
        sources: Optional[list[str]] = None,
        countries: Optional[list[str]] = None,
        currencies: Optional[list[str]] = None,
        session_buckets: Optional[list[str]] = None,
        statuses: Optional[list[str]] = None,
        importance_min: Optional[int] = None,
    ) -> list[Any]: ...


_RUNTIME_DEGRADE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    OSError,
    RuntimeError,
)


class EconomicDecayService:
    """Single port for economic event decay queries.

    Thread-safe; cache uses an internal lock.
    """

    def __init__(
        self,
        provider: EconomicEventsProvider,
        policy: SignalEconomicPolicy,
        *,
        cache_ttl_seconds: float = 60.0,
        cache_max_entries: int = 128,
    ) -> None:
        self._provider = provider
        self._policy = policy
        self._cache_ttl = float(cache_ttl_seconds)
        self._cache_max = int(cache_max_entries)
        self._cache: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()

    def decay_factor(self, symbol: str, *, at_time: datetime) -> float:
        """Return decay in [0.0, 1.0]; 1.0 means no influence.

        Exception contract (enforced by tests):
        - policy.enabled=False / provider missing → 1.0 (designed degrade, no log)
        - provider raises ConnectionError/TimeoutError/OSError/RuntimeError
              → 1.0 + WARNING log; result NOT cached
        - provider raises NameError/AttributeError/ImportError/TypeError/...
              → propagated (fail-fast, surfaces coding bugs immediately)
        """
        if not self._policy.enabled or self._provider is None:
            return 1.0

        cached = self._read_cache(symbol)
        if cached is not None:
            return cached

        try:
            decay = self._query(symbol, at_time)
        except _RUNTIME_DEGRADE_EXCEPTIONS as exc:
            logger.warning(
                "EconomicDecayService runtime degrade for %s: %s: %s",
                symbol,
                type(exc).__name__,
                exc,
            )
            return 1.0

        self._write_cache(symbol, decay)
        return decay

    def has_blocking_event(
        self, symbol: str, *, at_time: datetime
    ) -> tuple[bool, str]:
        """Hard-block check using filter_window; returns (blocked, reason)."""
        if not self._policy.enabled or self._provider is None:
            return False, ""
        try:
            from src.calendar import infer_symbol_context

            context = infer_symbol_context(symbol)
            events = self._provider.get_events(
                start_time=at_time
                - timedelta(minutes=self._policy.filter_window.lookback_minutes),
                end_time=at_time
                + timedelta(minutes=self._policy.filter_window.lookahead_minutes),
                limit=50,
                countries=context["countries"] or None,
                currencies=context["currencies"] or None,
                statuses=list(_SIGNAL_EVENT_STATUSES),
                importance_min=self._policy.filter_window.importance_min,
            )
        except _RUNTIME_DEGRADE_EXCEPTIONS as exc:
            logger.warning(
                "EconomicDecayService.has_blocking_event runtime degrade for %s: %s: %s",
                symbol,
                type(exc).__name__,
                exc,
            )
            return False, ""
        if events:
            return True, "economic_event_block"
        return False, ""

    def _query(self, symbol: str, at_time: datetime) -> float:
        from src.calendar import infer_symbol_context

        context = infer_symbol_context(symbol)
        events = self._provider.get_events(
            start_time=at_time
            - timedelta(minutes=self._policy.query_window.lookback_minutes),
            end_time=at_time
            + timedelta(minutes=self._policy.query_window.lookahead_minutes),
            limit=5,
            countries=context["countries"] or None,
            currencies=context["currencies"] or None,
            statuses=list(_SIGNAL_EVENT_STATUSES),
            importance_min=self._policy.query_window.importance_min,
        )
        if not events:
            return 1.0

        best_delta: float | None = None
        for evt in events:
            event_time = getattr(evt, "event_time", None)
            if event_time is None:
                continue
            delta = (event_time - at_time).total_seconds() / 60.0
            if best_delta is None or abs(delta) < abs(best_delta):
                best_delta = delta
        if best_delta is None:
            return 1.0
        return self._policy.decay_for_delta(best_delta)

    def _read_cache(self, symbol: str) -> float | None:
        now_mono = _time.monotonic()
        with self._lock:
            entry = self._cache.get(symbol)
            if entry is not None and now_mono < entry["expires_at"]:
                return entry["decay"]
            return None

    def _write_cache(self, symbol: str, decay: float) -> None:
        now_mono = _time.monotonic()
        with self._lock:
            self._cache[symbol] = {
                "decay": decay,
                "expires_at": now_mono + self._cache_ttl,
            }
            if len(self._cache) > self._cache_max:
                expired = [
                    k for k, v in self._cache.items() if now_mono >= v["expires_at"]
                ]
                for k in expired:
                    del self._cache[k]
                if len(self._cache) > self._cache_max:
                    by_age = sorted(
                        self._cache.items(), key=lambda kv: kv[1]["expires_at"]
                    )
                    for k, _ in by_age[: len(self._cache) - self._cache_max]:
                        del self._cache[k]
```

- [ ] **Step 2.2: 运行测试确认全绿**

Run: `pytest tests/calendar/test_economic_decay.py -v`
Expected: 12 passed

- [ ] **Step 2.3: Commit**

```bash
git add src/calendar/economic_decay.py
git commit -m "feat(calendar): EconomicDecayService — single decay query port

- 显式 at_time 注入，禁止 datetime.now()
- 异常分层：设计降级 / 运行时降级 + WARNING / 编码错误透传
- per-symbol 短 TTL cache + 容量淘汰
- has_blocking_event 用 filter_window；decay_factor 用 query_window"
```

---

## Task 3: 上提 `infer_symbol_context` 为 calendar 公共 API

**Files:**
- Modify: `src/calendar/__init__.py`

- [ ] **Step 3.1: 加 lazy export**

替换 `src/calendar/__init__.py` 内容为：

```python
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .economic_calendar.trade_guard import infer_symbol_context
    from .economic_decay import EconomicDecayService
    from .read_only_provider import ReadOnlyEconomicCalendarProvider
    from .service import EconomicCalendarService

__all__ = [
    "EconomicCalendarService",
    "EconomicDecayService",
    "ReadOnlyEconomicCalendarProvider",
    "infer_symbol_context",
]


def __getattr__(name: str):
    if name == "EconomicCalendarService":
        from .service import EconomicCalendarService

        return EconomicCalendarService
    if name == "EconomicDecayService":
        from .economic_decay import EconomicDecayService

        return EconomicDecayService
    if name == "ReadOnlyEconomicCalendarProvider":
        from .read_only_provider import ReadOnlyEconomicCalendarProvider

        return ReadOnlyEconomicCalendarProvider
    if name == "infer_symbol_context":
        from .economic_calendar.trade_guard import infer_symbol_context

        return infer_symbol_context
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

- [ ] **Step 3.2: 验证导入路径可用**

Run: `python -c "from src.calendar import EconomicDecayService, infer_symbol_context; print(infer_symbol_context('XAUUSD'))"`
Expected: 输出含 `currencies=['USD']` / `countries=['United States']` 之类。

- [ ] **Step 3.3: 重跑端口测试确保未破坏**

Run: `pytest tests/calendar/test_economic_decay.py -v`
Expected: 12 passed

- [ ] **Step 3.4: Commit**

```bash
git add src/calendar/__init__.py
git commit -m "refactor(calendar): export infer_symbol_context + EconomicDecayService as public API

- 消除跨域 lazy import 子包私有路径的 ADR-006 违反"
```

---

## Task 4: AppContainer 新增 `economic_decay_service` 字段

**Files:**
- Modify: `src/app_runtime/container.py`

- [ ] **Step 4.1: 在 `__slots__` 的 Calendar 段加字段**

修改 `src/app_runtime/container.py:87-89`，把：

```python
        # Calendar
        "economic_calendar_service",
        "market_impact_analyzer",
```

替换为：

```python
        # Calendar
        "economic_calendar_service",
        "economic_decay_service",
        "market_impact_analyzer",
```

- [ ] **Step 4.2: 在 `__init__` 同步初始化**

修改 `src/app_runtime/container.py:145-146`，把：

```python
        self.economic_calendar_service: Optional[EconomicCalendarService] = None
        self.market_impact_analyzer: Optional[Any] = None
```

替换为（先在文件顶部 import 区追加 `from src.calendar import EconomicDecayService`）：

```python
        self.economic_calendar_service: Optional[EconomicCalendarService] = None
        self.economic_decay_service: Optional[EconomicDecayService] = None
        self.market_impact_analyzer: Optional[Any] = None
```

并在文件顶部 `from src.calendar import EconomicCalendarService` 改为：

```python
from src.calendar import EconomicCalendarService, EconomicDecayService
```

- [ ] **Step 4.3: 验证容器可实例化**

Run: `python -c "from src.app_runtime.container import AppContainer; c = AppContainer(); assert c.economic_decay_service is None; print('ok')"`
Expected: `ok`

- [ ] **Step 4.4: Commit**

```bash
git add src/app_runtime/container.py
git commit -m "feat(app_runtime): AppContainer adds economic_decay_service slot"
```

---

## Task 5: SignalRuntime 注入 `economic_decay_service`，删除 `_event_decay_cache`

**Files:**
- Modify: `src/signals/orchestration/runtime.py`

- [ ] **Step 5.1: 删除 `_event_decay_cache` 字段**

删除 `src/signals/orchestration/runtime.py:239-241` 三行（含注释）：

```python
        # 经济事件 decay 因子缓存：per-symbol 短 TTL，避免每个策略 decision 都查 DB。
        # key=symbol → {"decay": float, "expires_at": float (monotonic)}
        self._event_decay_cache: dict[str, dict[str, float]] = {}
```

- [ ] **Step 5.2: 加 service 字段 + setter + property**

在 `src/signals/orchestration/runtime.py:168-169`（`self._economic_calendar_service: Any | None = None` 之后）追加：

```python
        # 经济事件 decay service：由装配层通过 set_economic_decay_service() 显式注入。
        self._economic_decay_service: Any | None = None
```

并在 `set_economic_calendar_service` 之后（line 288 之后）追加：

```python
    @property
    def economic_decay_service(self) -> Any | None:
        """Expose explicitly injected economic decay service."""
        return self._economic_decay_service

    def set_economic_decay_service(self, service: Any | None) -> None:
        """Inject EconomicDecayService for confidence decay resolution."""
        self._economic_decay_service = service
```

- [ ] **Step 5.3: 验证 SignalRuntime 仍可导入**

Run: `python -c "from src.signals.orchestration.runtime import SignalRuntime; print('ok')"`
Expected: `ok`

- [ ] **Step 5.4: Commit**

```bash
git add src/signals/orchestration/runtime.py
git commit -m "refactor(signals): SignalRuntime injects EconomicDecayService

- 删除 _event_decay_cache 字段（cache 归属 service 内部）
- 新增 set_economic_decay_service / economic_decay_service property"
```

---

## Task 6: `runtime_evaluator` 改用 service，删除 4 个内部函数 + 改 `apply_confidence_adjustments` 签名

**Files:**
- Modify: `src/signals/orchestration/runtime_evaluator.py`
- Modify: `src/signals/orchestration/runtime.py`（同步调用方）

- [ ] **Step 6.1: 改 `apply_confidence_adjustments` 签名加 `event_time`**

打开 `src/signals/orchestration/runtime_evaluator.py`，在 line 5 把：

```python
from datetime import datetime, timedelta
```

保持不变（不再需要 `timezone`，因为 wall clock 用法将整体删除）。

然后把 `apply_confidence_adjustments` 函数（[runtime_evaluator.py:346-374](src/signals/orchestration/runtime_evaluator.py#L346-L374)）整体替换为：

```python
def apply_confidence_adjustments(
    runtime: "SignalRuntime",
    decision: Any,
    symbol: str,
    timeframe: str,
    strategy: str,
    scope: str,
    regime_metadata: dict[str, Any],
    event_time: datetime,
) -> Any:
    economic_decay_applied = False
    if scope == "intrabar":
        decay = runtime._strategy_intrabar_decay.get(
            strategy, runtime._intrabar_confidence_factor
        )
        decision = apply_intrabar_decay(decision, scope, decay)

    # 经济事件渐进降权：距离高影响事件越近，置信度衰减越大
    if decision.confidence > 0 and decision.direction in ("buy", "sell"):
        service = runtime.economic_decay_service
        event_decay = (
            service.decay_factor(symbol, at_time=event_time)
            if service is not None
            else 1.0
        )
        if event_decay < 1.0:
            economic_decay_applied = True
            adjusted = decision.confidence * event_decay
            trace = list(decision.confidence_trace)
            trace.append(("economic_event_decay", round(adjusted, 4)))
            decision = _dc.replace(
                decision,
                confidence=adjusted,
                confidence_trace=trace,
            )
    # 后续 confidence_floor 等逻辑保持不变 — 见原文件继续部分
```

完整替换需保留原函数 line 376 之后到函数结尾的所有代码（confidence_floor 处理）。先 Read 该文件确认结尾位置，再用 Edit 把 `apply_confidence_adjustments` 整段（含上下文足够长以唯一定位）替换。

- [ ] **Step 6.2: 删除 4 个内部函数 + 字面量重复**

删除以下整块（[runtime_evaluator.py:242-289](src/signals/orchestration/runtime_evaluator.py#L242-L289) + [:292-343](src/signals/orchestration/runtime_evaluator.py#L292-L343)）：

```python
_EVENT_DECAY_TTL_SECONDS: float = 60.0
_EVENT_DECAY_CACHE_MAX: int = 128
_SIGNAL_EVENT_STATUSES = (...)

def _compute_economic_event_decay(...): ...
def _resolve_eco_context(...): ...
def _query_symbol_event_decay(...): ...
```

注意 `_resolve_eco_context` 在 `_resolve_event_impact_forecast`（[:200-230](src/signals/orchestration/runtime_evaluator.py#L200-L230)）中也被调用——见 Task 7 顺手处理。

同时删除文件顶部已不需要的 import：

```python
from src.calendar.policy import SignalEconomicPolicy   # ← 若已无其他引用，删除
```

通过 `grep -n SignalEconomicPolicy src/signals/orchestration/runtime_evaluator.py` 验证。

- [ ] **Step 6.3: `evaluate_strategies` 内 `apply_confidence_adjustments` 调用补 event_time**

修改 `src/signals/orchestration/runtime_evaluator.py:134-136`，把：

```python
        decision = apply_confidence_adjustments(
            runtime, decision, symbol, timeframe, strategy, scope, regime_metadata
        )
```

替换为：

```python
        decision = apply_confidence_adjustments(
            runtime, decision, symbol, timeframe, strategy, scope, regime_metadata,
            event_time,
        )
```

- [ ] **Step 6.4: SignalRuntime 内同步调用方签名**

修改 `src/signals/orchestration/runtime.py:566-577`，把：

```python
    def _apply_confidence_adjustments(
        self,
        decision: Any,
        symbol: str,
        timeframe: str,
        strategy: str,
        scope: str,
        regime_metadata: dict[str, Any],
    ) -> Any:
        return _runtime_apply_confidence_adjustments(
            self, decision, symbol, timeframe, strategy, scope, regime_metadata
        )
```

替换为：

```python
    def _apply_confidence_adjustments(
        self,
        decision: Any,
        symbol: str,
        timeframe: str,
        strategy: str,
        scope: str,
        regime_metadata: dict[str, Any],
        event_time: datetime,
    ) -> Any:
        return _runtime_apply_confidence_adjustments(
            self, decision, symbol, timeframe, strategy, scope, regime_metadata,
            event_time,
        )
```

如有其他位置调用 `_apply_confidence_adjustments`，运行 `grep -n "_apply_confidence_adjustments\|apply_confidence_adjustments" src/` 一并修。

- [ ] **Step 6.5: 跑 typecheck 验证签名一致**

Run: `mypy src/signals/orchestration/runtime_evaluator.py src/signals/orchestration/runtime.py`
Expected: no errors

- [ ] **Step 6.6: 跑端口测试确保未污染**

Run: `pytest tests/calendar/test_economic_decay.py -v`
Expected: 12 passed

- [ ] **Step 6.7: ⚠ 暂不 commit**

不要现在 commit。`_resolve_eco_context` 已删，但 `_resolve_event_impact_forecast` 还在引用它，仓库当前是 import-broken 状态。继续做 Task 7 后一起提交。

如已运行 mypy 看到 `_resolve_event_impact_forecast` 报错 — 符合预期，下一 Task 修。

---

## Task 7: 顺手体检 `_resolve_event_impact_forecast` 异常分层

> ⚠️ **顺序约束**：Task 6 已删除 `_resolve_eco_context`，本 Task 必须**在 Task 6 commit 之前完成**——把 Task 6.1~6.6 的所有改动 + Task 7.1 一起放在同一 working tree，最后**合并 commit**（用 Task 7.3 的 commit 消息覆盖 Task 6.7 的）。否则 Task 6 commit 后仓库处于 import 失败状态。
>
> 可选简化：把 Task 6 与 Task 7 看作一个原子改动，跳过 Task 6.7 的 commit，直接到 Task 7.3 一次性 commit 所有 evaluator 改动。

**Files:**
- Modify: `src/signals/orchestration/runtime_evaluator.py`

- [ ] **Step 7.1: 替换函数**

`_resolve_eco_context` 已在 Task 6 删除，`_resolve_event_impact_forecast` 需要重写——它原依赖 `_resolve_eco_context` 拿 provider+policy。改为依赖 `runtime.economic_calendar_service` + `runtime.economic_decay_service.policy`（service 暴露 policy 只读属性，先在 Task 2 文件结尾追加 property）。

先在 `src/calendar/economic_decay.py` 的 `EconomicDecayService` 类追加：

```python
    @property
    def policy(self) -> SignalEconomicPolicy:
        return self._policy
```

然后改 `src/signals/orchestration/runtime_evaluator.py:200-230` 整段替换为：

```python
def _resolve_event_impact_forecast(
    runtime: "SignalRuntime",
    symbol: str,
    scope: str,
    event_time: datetime,
) -> dict[str, Any] | None:
    if scope != "confirmed":
        return None
    cache = runtime._event_impact_cache
    if event_time < cache.get("expires_at", event_time):
        return cache.get("data")

    service = runtime.economic_decay_service
    impact_analyzer = runtime.market_impact_analyzer
    eco_service = runtime.economic_calendar_service
    if service is None or impact_analyzer is None or eco_service is None:
        cache["data"] = None
        cache["expires_at"] = event_time + timedelta(minutes=5)
        return None

    try:
        upcoming_events = _get_upcoming_high_impact(
            eco_service,
            service.policy.filter_window.importance_min,
        )
    except (ConnectionError, TimeoutError, OSError, RuntimeError) as exc:
        logger.warning(
            "event impact forecast runtime degrade for %s: %s: %s",
            symbol,
            type(exc).__name__,
            exc,
        )
        cache["data"] = None
        cache["expires_at"] = event_time + timedelta(minutes=5)
        return None

    if upcoming_events:
        cache["data"] = impact_analyzer.get_impact_forecast(
            upcoming_events[0].event_name, symbol=symbol
        )
    else:
        cache["data"] = None
    cache["expires_at"] = event_time + timedelta(minutes=5)
    return cache.get("data")
```

注意：原代码 `if event_time >= cache.get("expires_at", event_time):` 进入 refresh 分支；新版本改为 `<` 跳过 refresh 直接返回缓存——逻辑等价但更清晰。Read 整个函数确认无遗漏的执行分支后再 Edit。

- [ ] **Step 7.2: 运行已有 runtime 测试**

Run: `pytest tests/signals/test_runtime_evaluator.py -v` （此时旧测试仍会因 `_compute_economic_event_decay` 已删而 ImportError，预期。Task 9 修测试。）
Expected: ImportError on monkeypatch path（confirms 旧测试需重写）

- [ ] **Step 7.3: 合并 commit（含 Task 6 全部改动）**

```bash
git add src/signals/orchestration/runtime_evaluator.py src/signals/orchestration/runtime.py src/calendar/economic_decay.py
git commit -m "refactor(signals): runtime_evaluator delegates decay + impact to EconomicDecayService

- 删除 _compute_economic_event_decay / _query_symbol_event_decay /
  _resolve_eco_context 与 _SIGNAL_EVENT_STATUSES / _EVENT_DECAY_TTL_SECONDS
  / _EVENT_DECAY_CACHE_MAX 重复字面量
- apply_confidence_adjustments 增加 event_time 必填参数（修复 wall clock bug）
- _resolve_event_impact_forecast 异常分层（fail-fast 编码错误，WARNING 降级）
- EconomicDecayService 暴露 policy 只读 property 供上层复用"
```

---

## Task 8: `EconomicEventFilter` 委托 service，去除 filters.py 重复

**Files:**
- Modify: `src/signals/execution/filters.py`

- [ ] **Step 8.1: 重写 `EconomicEventFilter`**

把 `src/signals/execution/filters.py:134-174` 整段替换为：

```python
@dataclass
class EconomicEventFilter:
    """Signal-domain economic event hard-block filter.

    Internal logic delegated to EconomicDecayService so that
    filter and decay share a single source of truth.
    """

    service: Any | None = None  # EconomicDecayService — typed Any to avoid circular import

    def check_trade_guard(
        self, symbol: str, utc_now: Optional[datetime] = None
    ) -> tuple[bool, str]:
        """Return (safe, reason). safe=True means trading allowed."""
        if self.service is None:
            return True, ""
        at_time = utc_now or datetime.now(timezone.utc)
        blocked, reason = self.service.has_blocking_event(symbol, at_time=at_time)
        if blocked:
            return False, reason
        return True, ""

    def is_safe_to_trade(self, symbol: str, utc_now: Optional[datetime] = None) -> bool:
        safe, _ = self.check_trade_guard(symbol, utc_now)
        return safe
```

- [ ] **Step 8.2: 删除 filters.py 中已迁移的字面量与 helper**

删除 `src/signals/execution/filters.py:33-44`：

```python
_SIGNAL_EVENT_STATUSES = (
    "scheduled",
    "imminent",
    "pending_release",
    "released",
)


def _symbol_context(symbol: str) -> dict[str, list[str]]:
    from src.calendar.economic_calendar.trade_guard import infer_symbol_context

    return infer_symbol_context(symbol)
```

并删除 `src/signals/execution/filters.py:22` `from src.calendar.policy import SignalEconomicPolicy`（如本文件已无其他引用，由 grep 验证）。

`EconomicEventsProvider` Protocol（[:47-59](src/signals/execution/filters.py#L47-L59)）仍由其他地方使用——保留或迁移到 `src/calendar/economic_decay.py`（已在 Task 2 复制定义）；删除 filters.py 中的 Protocol，所有引用改为 `from src.calendar.economic_decay import EconomicEventsProvider`。

`grep -rn "from src.signals.execution.filters import EconomicEventsProvider\|filters.EconomicEventsProvider" src/ tests/` 确认无引用后再删。

- [ ] **Step 8.3: 修复使用 `EconomicEventFilter.policy` 的诊断字段**

`src/signals/execution/filters.py:394-402` 现在通过 `self.economic_filter.policy` 暴露诊断 dict。改为：

```python
                "policy": (
                    {
                        "lookahead_minutes": self.economic_filter.service.policy.filter_window.lookahead_minutes,
                        "lookback_minutes": self.economic_filter.service.policy.filter_window.lookback_minutes,
                        "importance_min": self.economic_filter.service.policy.filter_window.importance_min,
                        "decay_pre_minutes": self.economic_filter.service.policy.decay_pre_minutes,
                        "decay_post_minutes": self.economic_filter.service.policy.decay_post_minutes,
                    }
                    if self.economic_filter.service is not None
                    else None
                ),
```

完整 `grep -n "economic_filter.policy\|economic_filter.provider" src/` 检查所有访问点，统一切到 `economic_filter.service.policy`。

- [ ] **Step 8.4: 跑 filters 现有测试（若存在）**

Run: `pytest tests/signals/execution/ -v` 或 `pytest tests/signals/ -k filter -v`
Expected: 现有 filter 测试若构造 `EconomicEventFilter(provider=..., policy=...)` 会失败——见 Step 8.5 修。

- [ ] **Step 8.5: 修测试构造方式**

`grep -rn "EconomicEventFilter(" tests/`，把所有 `EconomicEventFilter(provider=p, policy=pol)` 改为：

```python
from src.calendar.economic_decay import EconomicDecayService

EconomicEventFilter(service=EconomicDecayService(provider=p, policy=pol))
```

- [ ] **Step 8.6: 重跑 filter 测试**

Run: `pytest tests/signals/ -k filter -v`
Expected: PASS

- [ ] **Step 8.7: Commit**

```bash
git add src/signals/execution/filters.py tests/
git commit -m "refactor(signals): EconomicEventFilter delegates to EconomicDecayService

- 删除 _SIGNAL_EVENT_STATUSES / _symbol_context 与 runtime_evaluator 的重复
- filter 与 decay 共用同一 service 实例
- 诊断字段统一从 service.policy 读取"
```

---

## Task 9: factories + builder phase 装配 service

**Files:**
- Modify: `src/app_runtime/factories/signals.py`
- Modify: `src/app_runtime/builder_phases/signal.py`

- [ ] **Step 9.1: factories/signals.py 新增 `build_economic_decay_service`**

在 `src/app_runtime/factories/signals.py` 顶部 import 段加：

```python
from src.calendar import EconomicDecayService
```

在 `build_signal_filter_chain` 之前（line 213 之前）加：

```python
def build_economic_decay_service(
    economic_calendar_service,
    economic_config=None,
) -> EconomicDecayService:
    policy = build_signal_economic_policy(economic_config or get_economic_config())
    return EconomicDecayService(
        provider=economic_calendar_service,
        policy=policy,
    )
```

- [ ] **Step 9.2: 修改 `build_signal_filter_chain` 接收 service**

修改 `src/app_runtime/factories/signals.py:213-242` 函数签名为：

```python
def build_signal_filter_chain(
    signal_config,
    economic_decay_service: EconomicDecayService,
) -> SignalFilterChain:
    return SignalFilterChain(
        session_filter=SessionFilter(
            allowed_sessions=tuple(
                normalize_session_name(session)
                for session in signal_config.allowed_sessions.split(",")
                if session.strip()
            )
        ),
        session_transition_filter=SessionTransitionFilter(
            cooldown_minutes=signal_config.session_transition_cooldown_minutes,
        ),
        spread_filter=SpreadFilter(
            max_spread_points=signal_config.max_spread_points,
            session_max_spread_points=dict(signal_config.session_spread_limits),
        ),
        economic_filter=EconomicEventFilter(
            service=economic_decay_service,
        ),
        volatility_filter=VolatilitySpikeFilter(
            spike_multiplier=signal_config.volatility_atr_spike_multiplier,
        ),
        trend_exhaustion_filter=TrendExhaustionFilter(),
    )
```

- [ ] **Step 9.3: 主装配路径修改（line ~801）**

修改 `src/app_runtime/factories/signals.py:801`，把：

```python
    filter_chain = build_signal_filter_chain(signal_config, economic_calendar_service)
```

替换为：

```python
    economic_decay_service = build_economic_decay_service(economic_calendar_service)
    filter_chain = build_signal_filter_chain(signal_config, economic_decay_service)
```

并在 `signal_runtime.set_economic_calendar_service(economic_calendar_service)`（line 822）之后追加：

```python
    signal_runtime.set_economic_decay_service(economic_decay_service)
```

修改返回结构以暴露 `economic_decay_service`——按现有 factory 返回模式（dataclass 或 dict）追加字段。先 Read line 800-1100 确认返回方式。

- [ ] **Step 9.4: 热重载路径修改（line ~1099）**

修改 `src/app_runtime/factories/signals.py:1100-1110` 热重载分支，把：

```python
            signal_runtime.filter_chain = build_signal_filter_chain(
                signal_config,
                economic_calendar_service,
                economic_config,
            )
```

替换为：

```python
            new_decay_service = build_economic_decay_service(
                economic_calendar_service, economic_config
            )
            signal_runtime.filter_chain = build_signal_filter_chain(
                signal_config, new_decay_service
            )
            signal_runtime.set_economic_decay_service(new_decay_service)
```

- [ ] **Step 9.5: builder_phases/signal.py 接通 container.economic_decay_service**

`src/app_runtime/builder_phases/signal.py:220, 280` 两处 `build_signal_filter_chain(...)` 调用都需要先构造 service。

Read 该文件 line 200-300 确认装配序列后，按以下模式修改两处：

```python
# 旧
filter_chain = build_signal_filter_chain(
    signal_config,
    economic_calendar_service=container.economic_calendar_service,
)

# 新
economic_decay_service = build_economic_decay_service(
    container.economic_calendar_service,
)
container.economic_decay_service = economic_decay_service
filter_chain = build_signal_filter_chain(
    signal_config,
    economic_decay_service=economic_decay_service,
)
```

并在文件顶部加：

```python
from src.app_runtime.factories.signals import build_economic_decay_service
```

- [ ] **Step 9.6: 冒烟测试**

Run: `pytest tests/smoke/ -v -x`
Expected: PASS（或现有失败与本改动无关）

Run: `pytest tests/app_runtime/ -v`
Expected: PASS

- [ ] **Step 9.7: Commit**

```bash
git add src/app_runtime/factories/signals.py src/app_runtime/builder_phases/signal.py
git commit -m "feat(app_runtime): wire EconomicDecayService into builder + hot reload

- factories/signals.py: build_economic_decay_service + filter_chain 注入
- builder_phases/signal.py: container.economic_decay_service 装配
- 热重载路径同步重建 service 并 set 到 SignalRuntime"
```

---

## Task 10: 重写 `tests/signals/test_runtime_evaluator.py`，禁用桩本体 monkeypatch

**Files:**
- Modify: `tests/signals/test_runtime_evaluator.py`

- [ ] **Step 10.1: 替换整个文件**

用以下内容覆盖 `tests/signals/test_runtime_evaluator.py`：

```python
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.signals.models import SignalDecision
from src.signals.orchestration.runtime_evaluator import apply_confidence_adjustments


class _StubDecayService:
    def __init__(self, decay: float) -> None:
        self._decay = decay
        self.calls: list[tuple[str, datetime]] = []

    def decay_factor(self, symbol: str, *, at_time: datetime) -> float:
        self.calls.append((symbol, at_time))
        return self._decay


class _RuntimeStub:
    def __init__(self, *, decay: float) -> None:
        self._strategy_intrabar_decay: dict[str, float] = {}
        self._intrabar_confidence_factor = 0.85
        self.confidence_floor = 0.10
        self._economic_decay_service: Any = _StubDecayService(decay)

    @property
    def economic_decay_service(self) -> Any:
        return self._economic_decay_service


def _decision(*, confidence: float) -> SignalDecision:
    return SignalDecision(
        strategy="structured_breakout_follow",
        symbol="XAUUSD",
        timeframe="M15",
        direction="buy",
        confidence=confidence,
        reason="test",
        timestamp=datetime(2026, 4, 13, 8, 0, tzinfo=timezone.utc),
        confidence_trace=[("raw", confidence)],
    )


_EVENT_TIME = datetime(2026, 4, 13, 8, 0, tzinfo=timezone.utc)


def test_economic_decay_is_not_revived_by_confidence_floor() -> None:
    runtime = _RuntimeStub(decay=0.0)
    adjusted = apply_confidence_adjustments(
        runtime,
        _decision(confidence=0.42),
        "XAUUSD",
        "M15",
        "structured_breakout_follow",
        "confirmed",
        {},
        _EVENT_TIME,
    )
    assert adjusted.confidence == 0.0
    assert adjusted.confidence_trace[-1] == ("economic_event_decay", 0.0)


def test_confidence_floor_still_applies_without_economic_decay() -> None:
    runtime = _RuntimeStub(decay=1.0)
    adjusted = apply_confidence_adjustments(
        runtime,
        _decision(confidence=0.04),
        "XAUUSD",
        "M15",
        "structured_breakout_follow",
        "confirmed",
        {},
        _EVENT_TIME,
    )
    assert adjusted.confidence == 0.10


def test_decay_service_called_with_event_time_not_wall_clock() -> None:
    """关键回归：apply_confidence_adjustments 必须把 event_time 透传给 service。"""
    runtime = _RuntimeStub(decay=0.5)
    apply_confidence_adjustments(
        runtime,
        _decision(confidence=0.40),
        "XAUUSD",
        "M15",
        "structured_breakout_follow",
        "confirmed",
        {},
        _EVENT_TIME,
    )
    service = runtime.economic_decay_service
    assert service.calls == [("XAUUSD", _EVENT_TIME)]


def test_no_decay_when_service_missing() -> None:
    runtime = _RuntimeStub(decay=0.0)
    runtime._economic_decay_service = None
    adjusted = apply_confidence_adjustments(
        runtime,
        _decision(confidence=0.42),
        "XAUUSD",
        "M15",
        "structured_breakout_follow",
        "confirmed",
        {},
        _EVENT_TIME,
    )
    # service 缺失 → decay 等价 1.0 → confidence 不变
    assert adjusted.confidence == 0.42
    assert all(t[0] != "economic_event_decay" for t in adjusted.confidence_trace)
```

- [ ] **Step 10.2: 跑测试**

Run: `pytest tests/signals/test_runtime_evaluator.py -v`
Expected: 4 passed

- [ ] **Step 10.3: Commit**

```bash
git add tests/signals/test_runtime_evaluator.py
git commit -m "test(signals): rewrite runtime_evaluator tests — inject stub service

- 禁用 monkeypatch 桩本体（旧模式让 P1 timezone bug 数月不被发现）
- 新增 event_time 透传回归测试
- 新增 service 缺失降级测试"
```

---

## Task 11: 全量测试 + 静态检查门禁

**Files:** N/A（仅运行验证命令）

- [ ] **Step 11.1: 跑相关单元测试**

```bash
pytest tests/calendar/ tests/signals/ tests/app_runtime/ -v
```

Expected: all passed; 任何失败回到对应 Task 修复。

- [ ] **Step 11.2: 类型检查**

```bash
mypy src/calendar/ src/signals/orchestration/ src/signals/execution/ src/app_runtime/
```

Expected: no errors

- [ ] **Step 11.3: 格式 + lint**

```bash
black src/calendar/economic_decay.py src/signals/orchestration/runtime_evaluator.py src/signals/orchestration/runtime.py src/signals/execution/filters.py src/app_runtime/container.py src/app_runtime/factories/signals.py src/app_runtime/builder_phases/signal.py tests/calendar/test_economic_decay.py tests/signals/test_runtime_evaluator.py
isort src/calendar/economic_decay.py src/signals/orchestration/runtime_evaluator.py src/signals/orchestration/runtime.py src/signals/execution/filters.py src/app_runtime/container.py src/app_runtime/factories/signals.py src/app_runtime/builder_phases/signal.py tests/calendar/test_economic_decay.py tests/signals/test_runtime_evaluator.py
flake8 src/calendar/economic_decay.py src/signals/orchestration/runtime_evaluator.py src/signals/orchestration/runtime.py src/signals/execution/filters.py src/app_runtime/container.py src/app_runtime/factories/signals.py src/app_runtime/builder_phases/signal.py tests/
```

Expected: clean

- [ ] **Step 11.4: 全量回归**

```bash
pytest -m "not slow" -x
```

Expected: all passed; 失败回到对应 Task 修复。

- [ ] **Step 11.5: Commit 任何 black/isort 自动调整**

如果有自动格式化变更：

```bash
git add -A
git commit -m "style: black/isort post-decay-service refactor"
```

---

## Task 12: ADR-011 + codebase-review §0g + CLAUDE.md 同步

**Files:**
- Modify: `docs/design/adr.md`
- Modify: `docs/codebase-review.md`
- Modify: `CLAUDE.md`

- [ ] **Step 12.1: 在 `docs/design/adr.md` ADR-010 之后追加 ADR-011**

在 `docs/design/adr.md:378`（"## ADR 模板"之前）插入：

```markdown
## ADR-011: Calendar 域提供 EconomicDecayService 单一端口 + 时间显式注入 + 异常分层

**日期**: 2026-04-26
**状态**: Accepted

### 背景

`signals/orchestration/runtime_evaluator.py:_query_symbol_event_decay` 因缺 `timezone` import 抛 NameError，被同函数所在调用层 `except Exception: decay = 1.0` 静默吞掉并写入 `runtime._event_decay_cache`（TTL 60s），导致**所有 symbol 经济事件渐进降权链路对全运行时失效数月未被发现**。同时审视发现：

1. **重复实现漂移**：`EconomicEventFilter.check_trade_guard` 与 `_query_symbol_event_decay` 各自组装 calendar 查询参数、各自维护 `_SIGNAL_EVENT_STATUSES` 字面量、各自跨域 lazy import `infer_symbol_context`——一份对一份错。
2. **时间源错误**：`_query_symbol_event_decay` 用 `datetime.now()` 而非链路已有的 `event_time`，导致 backtest/replay 场景概念失真。
3. **异常处理失序**：`except Exception` 把编码错误（NameError/AttributeError）和真实降级混作一谈（违反 CLAUDE.md §12）。
4. **测试桩本体绕过**：`tests/signals/test_runtime_evaluator.py` 通过 `monkeypatch.setattr(..._compute_economic_event_decay, lambda...)` 桩掉被测函数，等于零覆盖。

### 决策

1. **新建 `src/calendar/economic_decay.py:EconomicDecayService`** 作为 calendar 域唯一对外端口，处理"给定 symbol+at_time → decay 因子（[0,1]）"与硬阻断查询。
2. **`at_time` 强制显式注入**：禁止 `datetime.now()`。signal 域传 `event_time`；backtest/replay 传历史时刻。
3. **异常三段分层**：
   - 设计降级（policy 关闭 / provider 缺失） → 返回 1.0，无日志
   - 运行时降级（ConnectionError/TimeoutError/OSError/RuntimeError） → 返回 1.0 + WARNING 日志，**不写缓存**
   - 编码错误（NameError/AttributeError/ImportError/TypeError 等） → 直接传播，fail-fast
4. **filter 与 evaluator 共用同一 service 实例**：`EconomicEventFilter` 改为 `service: EconomicDecayService` 字段，内部委托 `has_blocking_event`，消除复制粘贴漂移。
5. **测试架构修正**：禁用桩被测函数本体的模式，改为注入 stub `EconomicDecayService`；新增 fail-fast 反向测试与 timezone 回归测试，结构性阻止同类静默失效再发生。

### 后果

- ✅ P1 静默失效根因彻底消除（不只是补 import，而是删掉滋生 bug 的复制粘贴模式）
- ✅ live/backtest 经济事件衰减语义统一（at_time 注入）
- ✅ ADR-006 边界规范进一步落实（signal 不再跨域组装 calendar 查询参数）
- ✅ CLAUDE.md §12 异常分层要求获得机制级支持
- ⚠️ 修改半径：3 个域、9 个文件、2 个测试改写、1 个 ADR、1 条 codebase-review 状态

### 不可回退点

- `EconomicDecayService.decay_factor` 签名 `at_time` keyword-only 必填（防止未来再退回 `datetime.now()`）
- `EconomicEventFilter` 不再持有 `provider` / `policy` 字段，只持有 `service`（防止再生重复实现）
- `tests/calendar/test_economic_decay.py::test_coding_error_propagates_fail_fast` 反向测试常驻，禁止改回 `except Exception` 兜底
```

- [ ] **Step 12.2: 在 `docs/codebase-review.md` 加 §0g**

在 `docs/codebase-review.md:52`（"## 0f. 2026-04-22 ..."之前）插入：

```markdown
## 0g. 2026-04-26 经济事件衰减边界整改（ADR-011）

### 触发

`signals/orchestration/runtime_evaluator.py:_query_symbol_event_decay` 缺 `timezone` import → NameError 被 `except Exception` 吞 + 缓存 1.0 → 所有 symbol 渐进降权静默失效（疑似数月）。同步审视发现复制粘贴漂移、时间源错误、测试桩本体三连根因。

### 边界变化

- **新增**：`src/calendar/economic_decay.py:EconomicDecayService` 作为 calendar 域唯一对外 decay/blocking 查询端口
- **删除**：`signals/orchestration/runtime_evaluator.py` 中 `_compute_economic_event_decay` / `_query_symbol_event_decay` / `_resolve_eco_context` / `_SIGNAL_EVENT_STATUSES` / `_EVENT_DECAY_TTL_SECONDS` / `_EVENT_DECAY_CACHE_MAX`
- **删除**：`signals/execution/filters.py` 中 `_SIGNAL_EVENT_STATUSES` / `_symbol_context` 与重复 `EconomicEventsProvider` Protocol（迁移至 `calendar/economic_decay.py`）
- **删除**：`SignalRuntime._event_decay_cache` 字段（cache 归属 service 内部）
- **变更**：`apply_confidence_adjustments` 签名增加必填 `event_time` 参数；`EconomicEventFilter` 字段从 `(provider, policy)` 改为 `(service,)`
- **新增端口**：`SignalRuntime.set_economic_decay_service` / `economic_decay_service` property
- **新增 container 字段**：`AppContainer.economic_decay_service`
- **公共 API 上提**：`infer_symbol_context` 与 `EconomicDecayService` 通过 `src/calendar/__init__.py` 暴露

### 异常分层契约（CLAUDE.md §12 落实）

| 场景 | 行为 |
|---|---|
| policy 关闭 / provider 缺失 | 返回 1.0，无日志（设计降级）|
| `ConnectionError/TimeoutError/OSError/RuntimeError` | 返回 1.0 + `WARNING` 日志，**不写缓存** |
| `NameError/AttributeError/ImportError/TypeError` 等 | 直接传播，fail-fast |

### 未决兼容项

无。本次改动一次到位删除所有兼容路径，无双轨并行。

### 减少边界泄漏的方式

signal 域不再组装 calendar 查询参数 / 不再跨域 lazy import 子包私有路径 / 不再各自维护 cache。signal/risk/backtest 三域只与 `EconomicDecayService` 单一端口对话。
```

- [ ] **Step 12.3: 更新 `CLAUDE.md` 模块路径表**

在 `CLAUDE.md` "经济日历" 表（搜索 `EconomicCalendarService | src/calendar/service.py`）追加一行：

```markdown
| EconomicDecayService（decay/blocking 查询端口） | `src/calendar/economic_decay.py` |
```

并在 "Known Issues" 之后的 "已沉淀的设计决策"列表追加：

```markdown
- ADR-011: Calendar 域提供 EconomicDecayService 单一端口 + 时间显式注入 + 异常分层
```

- [ ] **Step 12.4: 验证文档无格式错误**

Run: `python -c "import re; [open(p).read() for p in ['docs/design/adr.md', 'docs/codebase-review.md', 'CLAUDE.md']]; print('ok')"`
Expected: `ok`

- [ ] **Step 12.5: Commit**

```bash
git add docs/design/adr.md docs/codebase-review.md CLAUDE.md
git commit -m "docs(adr-011): EconomicDecayService 边界整改 + 异常分层契约

- ADR-011 沉淀决策与不可回退点
- codebase-review §0g 状态台账登记
- CLAUDE.md 模块路径表 + ADR 索引同步"
```

---

## 完成验证清单

执行完所有 task 后，逐项核对：

- [ ] `grep -rn "datetime.now(timezone.utc)" src/signals/orchestration/runtime_evaluator.py` → 无命中（wall clock 用法已根除）
- [ ] `grep -rn "_compute_economic_event_decay\|_query_symbol_event_decay\|_resolve_eco_context\|_event_decay_cache" src/` → 无命中（旧实现已删干净）
- [ ] `grep -rn "_SIGNAL_EVENT_STATUSES" src/` → 仅 `src/calendar/economic_decay.py` 一处
- [ ] `grep -rn "from src.calendar.economic_calendar.trade_guard import infer_symbol_context" src/` → 无命中（统一从 `src.calendar` 导入）
- [ ] `grep -rn "except Exception" src/signals/orchestration/runtime_evaluator.py src/calendar/economic_decay.py` → 无命中（异常已分层）
- [ ] `pytest tests/calendar/test_economic_decay.py::test_coding_error_propagates_fail_fast` 单测通过（fail-fast 反向锁死）
- [ ] `pytest tests/calendar/test_economic_decay.py::test_query_window_uses_at_time_not_wall_clock` 单测通过（timezone/时间源回归锁死）
- [ ] `pytest tests/signals/test_runtime_evaluator.py::test_decay_service_called_with_event_time_not_wall_clock` 单测通过（event_time 透传锁死）
- [ ] 全量 `pytest -m "not slow"` 通过
- [ ] `mypy src/` 无 strict 错误
