"""Pending entry 填单语义单测：验证 look-ahead 修复。

回归覆盖：之前版本用 `bar.low <= entry_high and bar.high >= entry_low` 判触发，
再用 `bar.close` 成交 → 相当于用了未来 bar 极值才下单。修复后必须按挂单方向
单向判断，并在跳空场景用 bar.open 成交。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.backtesting.engine.signals import _resolve_pending_fill
from src.clients.mt5_market import OHLC


def _make_bar(o: float, h: float, l: float, c: float) -> OHLC:
    return OHLC(
        symbol="XAUUSD",
        timeframe="M15",
        time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        open=o,
        high=h,
        low=l,
        close=c,
        volume=0,
    )


class TestBuyLimit:
    """Buy limit：价格回落至 zone 上边界才触发。"""

    def test_not_triggered_when_bar_stays_above(self) -> None:
        # zone [1990, 1995]，bar 全程在 2000+ 上方
        bar = _make_bar(2005, 2010, 2000, 2008)
        assert _resolve_pending_fill("buy", "limit", 1990, 1995, bar) is None

    def test_fill_at_upper_edge_when_dipped_in(self) -> None:
        # bar.low 跌入 zone，首次触及 entry_high（1995）
        bar = _make_bar(2000, 2002, 1993, 1998)
        assert _resolve_pending_fill("buy", "limit", 1990, 1995, bar) == 1995

    def test_fill_at_open_when_gap_below_zone(self) -> None:
        # 跳空：bar.open 已在 zone 内 → 按 open 成交（realistic broker）
        bar = _make_bar(1992, 1996, 1988, 1993)
        assert _resolve_pending_fill("buy", "limit", 1990, 1995, bar) == 1992

    def test_fill_at_open_when_gap_below_zone_entirely(self) -> None:
        # 跳空到 zone 下方：open 就应该成交（无法在 entry_high 成交）
        bar = _make_bar(1985, 1989, 1980, 1987)
        # 实际上 bar.low > entry_high 不成立；bar.low=1980 <= 1995 触发
        # 但 open=1985 < entry_high=1995 → min(1985, 1995) = 1985
        assert _resolve_pending_fill("buy", "limit", 1990, 1995, bar) == 1985


class TestSellLimit:
    """Sell limit：价格上涨至 zone 下边界才触发。"""

    def test_not_triggered_when_bar_stays_below(self) -> None:
        bar = _make_bar(1985, 1989, 1980, 1987)
        assert _resolve_pending_fill("sell", "limit", 1990, 1995, bar) is None

    def test_fill_at_lower_edge_when_rallied_in(self) -> None:
        bar = _make_bar(1985, 1992, 1984, 1988)
        assert _resolve_pending_fill("sell", "limit", 1990, 1995, bar) == 1990

    def test_fill_at_open_when_gap_above_zone(self) -> None:
        # 跳空：bar.open 已在 zone 内 → 按 open 成交
        bar = _make_bar(1993, 1998, 1991, 1996)
        assert _resolve_pending_fill("sell", "limit", 1990, 1995, bar) == 1993


class TestBuyStop:
    """Buy stop：价格突破至 zone 下边界才触发（追多）。"""

    def test_not_triggered_when_below_zone(self) -> None:
        bar = _make_bar(1985, 1989, 1980, 1987)
        assert _resolve_pending_fill("buy", "stop", 1990, 1995, bar) is None

    def test_fill_at_lower_edge_on_breakout(self) -> None:
        bar = _make_bar(1985, 1992, 1984, 1991)
        assert _resolve_pending_fill("buy", "stop", 1990, 1995, bar) == 1990

    def test_fill_at_open_when_gap_above_zone(self) -> None:
        # 跳空突破：open > entry_low → 按 open 成交（对 buyer 不利但真实）
        bar = _make_bar(1998, 2005, 1997, 2002)
        assert _resolve_pending_fill("buy", "stop", 1990, 1995, bar) == 1998


class TestSellStop:
    """Sell stop：价格跌破 zone 上边界才触发（追空）。"""

    def test_not_triggered_when_above_zone(self) -> None:
        bar = _make_bar(2005, 2010, 2000, 2008)
        assert _resolve_pending_fill("sell", "stop", 1990, 1995, bar) is None

    def test_fill_at_upper_edge_on_breakdown(self) -> None:
        bar = _make_bar(2000, 2002, 1993, 1994)
        assert _resolve_pending_fill("sell", "stop", 1990, 1995, bar) == 1995

    def test_fill_at_open_when_gap_below_zone(self) -> None:
        bar = _make_bar(1987, 1989, 1980, 1985)
        assert _resolve_pending_fill("sell", "stop", 1990, 1995, bar) == 1987


class TestLookAheadRegression:
    """历史 bug 回归：bar 仅以 close 远离 zone 却因极值触发不应吃 close 价。"""

    def test_buy_limit_close_far_from_trigger_still_fills_at_edge(self) -> None:
        # bar 从 2000 冲高到 2020 再跌回 1993（刺入 zone）收在 2015
        # 旧 bug：触发成立 + 用 bar.close=2015 成交 → 相当于在 1993 时点预知后续反弹
        # 修复后：应成交在 entry_high=1995（首次触及上边界）
        bar = _make_bar(2000, 2020, 1993, 2015)
        fill = _resolve_pending_fill("buy", "limit", 1990, 1995, bar)
        assert fill == 1995
        assert fill != bar.close

    def test_buy_stop_close_far_below_trigger_fills_at_edge(self) -> None:
        # bar 先突破 1990 冲高到 1998 再暴跌收在 1982
        # 旧 bug：用 bar.close=1982 成交 buy_stop → 比挂单价低 8 美元（不可能）
        # 修复后：成交在 entry_low=1990
        bar = _make_bar(1985, 1998, 1980, 1982)
        fill = _resolve_pending_fill("buy", "stop", 1990, 1995, bar)
        assert fill == 1990
        assert fill != bar.close


class TestUnknownCombination:
    def test_unknown_direction_returns_none(self) -> None:
        bar = _make_bar(2000, 2010, 1990, 2005)
        assert _resolve_pending_fill("hold", "limit", 1990, 1995, bar) is None

    def test_unknown_entry_type_returns_none(self) -> None:
        bar = _make_bar(2000, 2010, 1990, 2005)
        assert _resolve_pending_fill("buy", "market", 1990, 1995, bar) is None
