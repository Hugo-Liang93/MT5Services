"""prior_day_levels indicator — 前一交易日 OHLC 水平。

供 StructuredPriorDayRetest 等 day-trading edge 策略使用：retail trader
普遍参考"昨日高/低/收盘"作为 SR levels，触及 + 反转 K 线 = 经典反转入场。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.indicators.core.prior_day_levels import prior_day_levels


@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def _bar(t: datetime, h: float, l: float, c: float, o: float | None = None) -> Bar:
    return Bar(
        time=t,
        open=o if o is not None else c,
        high=h,
        low=l,
        close=c,
        volume=1000.0,
    )


def test_empty_bars_returns_empty() -> None:
    assert prior_day_levels([], {}) == {}


def test_single_day_only_returns_empty() -> None:
    """只有当日 bars 没有"昨日"概念。"""
    day = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
    bars = [
        _bar(day + timedelta(hours=i), h=100 + i, l=99 + i, c=99.5 + i)
        for i in range(5)
    ]
    assert prior_day_levels(bars, {}) == {}


def test_two_days_extracts_prev_day_high_low_close() -> None:
    """昨日 bars + 今日 bars → 输出昨日 H/L/C。"""
    d1 = datetime(2026, 4, 14, tzinfo=timezone.utc)
    d2 = datetime(2026, 4, 15, tzinfo=timezone.utc)
    bars = [
        # 昨日：H=110, L=100, C=105
        _bar(d1 + timedelta(hours=0), h=105, l=100, c=102),
        _bar(d1 + timedelta(hours=12), h=110, l=103, c=108),  # day high
        _bar(d1 + timedelta(hours=23), h=109, l=104, c=105),  # day close
        # 今日
        _bar(d2 + timedelta(hours=1), h=106, l=104, c=105.5),
    ]
    out = prior_day_levels(bars, {})
    assert out["prev_day_high"] == 110.0
    assert out["prev_day_low"] == 100.0
    assert out["prev_day_close"] == 105.0
    assert out["prev_day_range"] == 10.0


def test_skips_weekend_uses_last_available_prior_day() -> None:
    """周一 bar 的"昨日"应是上周五（跳过周六周日）。"""
    fri = datetime(2026, 4, 10, tzinfo=timezone.utc)  # 周五
    mon = datetime(2026, 4, 13, tzinfo=timezone.utc)  # 周一
    bars = [
        _bar(fri + timedelta(hours=10), h=120, l=115, c=118),
        _bar(fri + timedelta(hours=22), h=119, l=116, c=117),  # Friday close
        _bar(mon + timedelta(hours=1), h=118, l=116, c=117.5),
    ]
    out = prior_day_levels(bars, {})
    # Friday H=120, L=115, C=117
    assert out["prev_day_high"] == 120.0
    assert out["prev_day_low"] == 115.0
    assert out["prev_day_close"] == 117.0


def test_three_days_uses_only_immediate_prior_day() -> None:
    """有 D-2 / D-1 / D 时，只用 D-1（最近的过去一日）。"""
    d_minus_2 = datetime(2026, 4, 13, tzinfo=timezone.utc)
    d_minus_1 = datetime(2026, 4, 14, tzinfo=timezone.utc)
    today = datetime(2026, 4, 15, tzinfo=timezone.utc)
    bars = [
        # D-2: H=200 L=190（更高更低，但不应被取）
        _bar(d_minus_2 + timedelta(hours=10), h=200, l=190, c=195),
        # D-1: H=110 L=100（应被取）
        _bar(d_minus_1 + timedelta(hours=10), h=110, l=100, c=105),
        _bar(d_minus_1 + timedelta(hours=22), h=108, l=102, c=107),
        # D
        _bar(today + timedelta(hours=1), h=109, l=106, c=108),
    ]
    out = prior_day_levels(bars, {})
    assert out["prev_day_high"] == 110.0
    assert out["prev_day_low"] == 100.0
    # prev_day_close = D-1 最后一根的 close
    assert out["prev_day_close"] == 107.0


def test_prev_day_close_picks_last_bar_of_day() -> None:
    """prev_day_close 是 D-1 最后一根 bar 的 close（不是 max/min）。"""
    yest = datetime(2026, 4, 14, tzinfo=timezone.utc)
    today = datetime(2026, 4, 15, tzinfo=timezone.utc)
    bars = [
        _bar(yest + timedelta(hours=0), h=100, l=99, c=99.5),
        _bar(yest + timedelta(hours=12), h=110, l=98, c=109),  # 中间 bar 价高
        _bar(yest + timedelta(hours=23, minutes=55), h=105, l=103, c=104.2),  # 收盘 bar
        _bar(today + timedelta(hours=1), h=105, l=104, c=104.5),
    ]
    out = prior_day_levels(bars, {})
    assert out["prev_day_close"] == 104.2  # 最后一根 close, not 109


def test_distance_helpers_use_current_close() -> None:
    """输出 prev_day_high - close / close - prev_day_low 让策略层无需重复算。"""
    yest = datetime(2026, 4, 14, tzinfo=timezone.utc)
    today = datetime(2026, 4, 15, tzinfo=timezone.utc)
    bars = [
        _bar(yest + timedelta(hours=10), h=110, l=100, c=105),
        _bar(today + timedelta(hours=1), h=108, l=106, c=107),
    ]
    out = prior_day_levels(bars, {})
    # close=107, prev_high=110 → distance to high = 3
    assert out["distance_to_prev_high"] == 3.0
    # close=107, prev_low=100 → distance to low = 7
    assert out["distance_to_prev_low"] == 7.0


def test_position_within_prev_range_normalized() -> None:
    """position_in_prev_range = (close - prev_low) / (prev_high - prev_low)，0~1。"""
    yest = datetime(2026, 4, 14, tzinfo=timezone.utc)
    today = datetime(2026, 4, 15, tzinfo=timezone.utc)
    bars = [
        _bar(yest + timedelta(hours=10), h=110, l=100, c=105),
        _bar(today + timedelta(hours=1), h=108, l=106, c=105),  # close=105
    ]
    out = prior_day_levels(bars, {})
    # (105 - 100) / (110 - 100) = 0.5
    assert out["position_in_prev_range"] == 0.5


def test_zero_range_safe_division() -> None:
    """昨日 high=low（全平 bar）→ position 退化为 0.5，distance 仍输出。"""
    yest = datetime(2026, 4, 14, tzinfo=timezone.utc)
    today = datetime(2026, 4, 15, tzinfo=timezone.utc)
    bars = [
        _bar(yest + timedelta(hours=10), h=100, l=100, c=100),
        _bar(today + timedelta(hours=1), h=101, l=99, c=100.5),
    ]
    out = prior_day_levels(bars, {})
    assert out["prev_day_range"] == 0.0
    assert out["position_in_prev_range"] == 0.5  # safe fallback
