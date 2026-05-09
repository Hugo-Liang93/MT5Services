"""SignalRuntime.latest_tick_features() T9 wiring 缓存 + evaluator 注入测试。

验证 plan §三 P0-1 修复：SignalRuntime 在 enqueue_tick_feature_snapshot 时
缓存 indicator-style 字典，confirmed 评估时通过 latest_tick_features() 端口
注入到 ctx.indicators["tick_features"]，让 PA._post_confidence_modifier
等 hook 真正生效。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.signals.orchestration.policy import SignalPolicy
from src.signals.orchestration.runtime import SignalRuntime, SignalTarget


class _DummySnapshotSource:
    def add_snapshot_listener(self, fn):
        pass

    def remove_snapshot_listener(self, fn):
        pass


class _DummyService:
    """SignalRuntime 的最小 service 桩——T9 wiring 测试不实际评估策略。"""

    def list_strategies(self):
        return []

    def get_strategy(self, name):
        return None

    @property
    def strategy_capabilities(self):
        return {}

    def strategy_capability_catalog(self):
        return []

    def strategy_requirements(self, strategy):
        return ()


def _make_snapshot(
    *,
    symbol: str = "XAUUSD",
    bid: float | None = 100.0,
    ask: float | None = 100.02,
    buy_pressure: float | None = 0.65,
    sell_pressure: float | None = 0.35,
    spread_points: float | None = 2.0,
    price_change_points: float | None = 1.0,
):
    """构造最小 TickFeatureSnapshot 测试桩（不依赖 dataclass 完整字段）。"""
    return SimpleNamespace(
        symbol=symbol,
        window_start_msc=1000,
        window_end_msc=2000,
        generated_at=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
        tick_count=10,
        bid=bid,
        ask=ask,
        last=None if bid is None or ask is None else (bid + ask) / 2,
        mid=None if bid is None or ask is None else (bid + ask) / 2,
        spread_points=spread_points,
        quote_age_ms=100,
        realized_range_points=8.0,
        price_change_points=price_change_points,
        buy_pressure=buy_pressure,
        sell_pressure=sell_pressure,
        status="healthy",
        reasons=(),
    )


def _make_runtime() -> SignalRuntime:
    """最小可运行 SignalRuntime（无策略；只测 cache 端口）。"""
    return SignalRuntime(
        service=_DummyService(),
        snapshot_source=_DummySnapshotSource(),
        targets=[],
        enable_confirmed_snapshot=False,
        policy=SignalPolicy(),
    )


def test_signal_runtime_caches_latest_tick_features_per_symbol() -> None:
    """enqueue_tick_feature_snapshot 必须缓存 indicator-style 字典 per symbol。"""
    runtime = _make_runtime()
    snapshot = _make_snapshot(buy_pressure=0.7, sell_pressure=0.3)

    runtime.enqueue_tick_feature_snapshot(snapshot)

    cached = runtime.latest_tick_features("XAUUSD")
    assert cached is not None
    assert cached.get("buy_pressure") == 0.7
    assert cached.get("sell_pressure") == 0.3
    assert cached.get("bid") == 100.0
    assert cached.get("ask") == 100.02


def test_signal_runtime_returns_none_for_unseen_symbol() -> None:
    """未推送过 snapshot 的 symbol 返回 None。"""
    runtime = _make_runtime()
    assert runtime.latest_tick_features("XAUUSD") is None
    assert runtime.latest_tick_features("EURUSD") is None


def test_signal_runtime_overwrites_cache_on_new_snapshot() -> None:
    """新 snapshot 覆盖旧缓存（per symbol）。"""
    runtime = _make_runtime()

    runtime.enqueue_tick_feature_snapshot(
        _make_snapshot(buy_pressure=0.6, sell_pressure=0.4)
    )
    runtime.enqueue_tick_feature_snapshot(
        _make_snapshot(buy_pressure=0.8, sell_pressure=0.2)
    )

    cached = runtime.latest_tick_features("XAUUSD")
    assert cached is not None
    assert cached["buy_pressure"] == 0.8
    assert cached["sell_pressure"] == 0.2


def test_signal_runtime_returns_independent_dict_copy() -> None:
    """返回的字典必须是 copy，调用方修改不污染内部缓存。"""
    runtime = _make_runtime()
    runtime.enqueue_tick_feature_snapshot(_make_snapshot(buy_pressure=0.7))

    cached_first = runtime.latest_tick_features("XAUUSD")
    assert cached_first is not None
    cached_first["buy_pressure"] = 0.0  # 试图污染

    cached_again = runtime.latest_tick_features("XAUUSD")
    assert cached_again is not None
    assert cached_again["buy_pressure"] == 0.7  # 未被污染


def test_signal_runtime_caches_tick_features_independently_per_symbol() -> None:
    """多 symbol 的缓存互不干扰。"""
    runtime = _make_runtime()
    runtime.enqueue_tick_feature_snapshot(
        _make_snapshot(symbol="XAUUSD", buy_pressure=0.7)
    )
    runtime.enqueue_tick_feature_snapshot(
        _make_snapshot(symbol="EURUSD", buy_pressure=0.4)
    )

    xau = runtime.latest_tick_features("XAUUSD")
    eur = runtime.latest_tick_features("EURUSD")
    assert xau is not None and xau["buy_pressure"] == 0.7
    assert eur is not None and eur["buy_pressure"] == 0.4
