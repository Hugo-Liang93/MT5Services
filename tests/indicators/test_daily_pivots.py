"""daily_pivots indicator — 经典浮动 pivot points (P/R1/S1/R2/S2)。

day-trading 经典 SR：retail 普遍参考 pivot levels 决策入场/出场。
公式：
    P  = (prev_H + prev_L + prev_C) / 3
    R1 = 2P - prev_L          S1 = 2P - prev_H
    R2 = P + (prev_H - prev_L) S2 = P - (prev_H - prev_L)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.indicators.core.daily_pivots import daily_pivots


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
    assert daily_pivots([], {}) == {}


def test_single_day_only_returns_empty() -> None:
    """无前一日 → 无法算 pivots。"""
    day = datetime(2026, 4, 15, tzinfo=timezone.utc)
    bars = [_bar(day + timedelta(hours=i), h=100 + i, l=99 + i, c=99.5 + i) for i in range(5)]
    assert daily_pivots(bars, {}) == {}


def test_classic_pivot_formula() -> None:
    """P/R1/S1/R2/S2 公式正确。

    prev: H=110 L=100 C=105 → P=(110+100+105)/3=105
    R1=2P-L=210-100=110, S1=2P-H=210-110=100
    R2=P+(H-L)=105+10=115, S2=P-(H-L)=105-10=95
    """
    yest = datetime(2026, 4, 14, tzinfo=timezone.utc)
    today = datetime(2026, 4, 15, tzinfo=timezone.utc)
    bars = [
        _bar(yest + timedelta(hours=10), h=110, l=100, c=108),
        _bar(yest + timedelta(hours=22), h=109, l=104, c=105),  # close
        _bar(today + timedelta(hours=1), h=106, l=104, c=105),
    ]
    out = daily_pivots(bars, {})
    assert out["pivot"] == 105.0
    assert out["r1"] == 110.0
    assert out["s1"] == 100.0
    assert out["r2"] == 115.0
    assert out["s2"] == 95.0


def test_distance_helpers_use_current_close() -> None:
    """输出 close 与每个 pivot level 的距离，方便策略层不重算。"""
    yest = datetime(2026, 4, 14, tzinfo=timezone.utc)
    today = datetime(2026, 4, 15, tzinfo=timezone.utc)
    bars = [
        _bar(yest + timedelta(hours=10), h=110, l=100, c=105),
        _bar(today + timedelta(hours=1), h=108, l=104, c=107),  # close=107
    ]
    out = daily_pivots(bars, {})
    # P=105, R1=110, S1=100, R2=115, S2=95
    # close=107 → dist_to_pivot = 107-105 = 2 (above)
    assert out["distance_to_pivot"] == 2.0
    assert out["distance_to_r1"] == -3.0  # close below R1
    assert out["distance_to_s1"] == 7.0  # close above S1


def test_nearest_level_returned() -> None:
    """nearest_level / nearest_distance 标识当前 close 最近的 pivot level。"""
    yest = datetime(2026, 4, 14, tzinfo=timezone.utc)
    today = datetime(2026, 4, 15, tzinfo=timezone.utc)
    bars = [
        _bar(yest + timedelta(hours=10), h=110, l=100, c=105),
        # close=109.5 → R1=110 距离 0.5（最近）
        _bar(today + timedelta(hours=1), h=110, l=109, c=109.5),
    ]
    out = daily_pivots(bars, {})
    assert out["nearest_level_name"] == "r1"
    assert abs(out["nearest_level_distance"] - 0.5) < 1e-9


def test_skips_weekend_gap() -> None:
    """周一 bar 的 pivot 来自上周五 OHLC（跳过周末）。"""
    fri = datetime(2026, 4, 10, tzinfo=timezone.utc)
    mon = datetime(2026, 4, 13, tzinfo=timezone.utc)
    bars = [
        _bar(fri + timedelta(hours=10), h=120, l=115, c=118),
        _bar(fri + timedelta(hours=22), h=119, l=116, c=117),
        _bar(mon + timedelta(hours=1), h=118, l=116, c=117),
    ]
    out = daily_pivots(bars, {})
    # Friday H=120, L=115, C=117 → P=(120+115+117)/3=117.333
    assert abs(out["pivot"] - 117.3333) < 1e-3
