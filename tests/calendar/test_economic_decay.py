# tests/calendar/test_economic_decay.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.calendar.economic_decay import EconomicDecayService
from src.calendar.policy import EconomicEventWindow, SignalEconomicPolicy


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
    service = EconomicDecayService(
        provider=provider, policy=_policy(decay_pre=30, decay_post=30)
    )
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


def test_runtime_failure_returns_one_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ConnectionError 等真实降级场景：返回 1.0 + WARNING 日志。"""
    provider = _StubProvider(side_effect=ConnectionError("DB unreachable"))
    service = EconomicDecayService(provider=provider, policy=_policy())
    with caplog.at_level(logging.WARNING, logger="src.calendar.economic_decay"):
        decay = service.decay_factor("XAUUSD", at_time=_AT_TIME)
    assert decay == 1.0
    assert any(
        "XAUUSD" in rec.message and "ConnectionError" in rec.message
        for rec in caplog.records
    )


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
    provider = _StubProvider(
        events=[_StubEvent(event_time=_AT_TIME + timedelta(minutes=2))]
    )
    service = EconomicDecayService(
        provider=provider,
        policy=_policy(
            hard_block_pre=5, hard_block_post=5, decay_pre=30, decay_post=30
        ),
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
