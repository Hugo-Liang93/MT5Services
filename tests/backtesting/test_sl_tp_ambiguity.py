"""SL/TP 同 bar 优先级回归测试。

修复目标：同 bar 内 TP 与 SL 都被触及时，旧实现优先判 TP 导致胜率虚高。
新实现：SL 获胜（保守），并处理跳空越界情况。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from src.backtesting.engine.portfolio import _resolve_hard_boundary_exit
from src.clients.mt5_market import OHLC


@dataclass
class _Pos:
    direction: str
    stop_loss: float
    take_profit: float


def _bar(o: float, h: float, l: float, c: float) -> OHLC:
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


class TestBuyPosition:
    def test_only_tp_hit(self) -> None:
        pos = _Pos("buy", stop_loss=1990.0, take_profit=2010.0)
        bar = _bar(2000, 2012, 1995, 2010)
        res = _resolve_hard_boundary_exit(bar, pos)
        assert res == (2010.0, "take_profit")

    def test_only_sl_hit_returns_none(self) -> None:
        # 只 SL 触发时交给 evaluate_exit 处理 trailing
        pos = _Pos("buy", stop_loss=1990.0, take_profit=2010.0)
        bar = _bar(2000, 2005, 1988, 1992)
        assert _resolve_hard_boundary_exit(bar, pos) is None

    def test_both_hit_sl_wins(self) -> None:
        # 关键回归：同 bar 双触时 SL 获胜（而非旧的 TP 优先）
        pos = _Pos("buy", stop_loss=1990.0, take_profit=2010.0)
        bar = _bar(2000, 2015, 1988, 2005)
        assert _resolve_hard_boundary_exit(bar, pos) == (1990.0, "stop_loss")

    def test_gap_down_below_sl_fills_at_open(self) -> None:
        pos = _Pos("buy", stop_loss=1990.0, take_profit=2010.0)
        bar = _bar(1985, 1988, 1980, 1986)
        assert _resolve_hard_boundary_exit(bar, pos) == (1985.0, "stop_loss")

    def test_gap_up_above_tp_fills_at_open(self) -> None:
        pos = _Pos("buy", stop_loss=1990.0, take_profit=2010.0)
        bar = _bar(2015, 2020, 2014, 2018)
        assert _resolve_hard_boundary_exit(bar, pos) == (2015.0, "take_profit")

    def test_no_hit_returns_none(self) -> None:
        pos = _Pos("buy", stop_loss=1990.0, take_profit=2010.0)
        bar = _bar(2000, 2005, 1995, 2002)
        assert _resolve_hard_boundary_exit(bar, pos) is None


class TestSellPosition:
    def test_only_tp_hit(self) -> None:
        pos = _Pos("sell", stop_loss=2010.0, take_profit=1990.0)
        bar = _bar(2000, 2005, 1988, 1990)
        assert _resolve_hard_boundary_exit(bar, pos) == (1990.0, "take_profit")

    def test_both_hit_sl_wins(self) -> None:
        pos = _Pos("sell", stop_loss=2010.0, take_profit=1990.0)
        bar = _bar(2000, 2012, 1988, 2005)
        assert _resolve_hard_boundary_exit(bar, pos) == (2010.0, "stop_loss")

    def test_gap_up_above_sl_fills_at_open(self) -> None:
        pos = _Pos("sell", stop_loss=2010.0, take_profit=1990.0)
        bar = _bar(2015, 2020, 2014, 2018)
        assert _resolve_hard_boundary_exit(bar, pos) == (2015.0, "stop_loss")

    def test_gap_down_below_tp_fills_at_open(self) -> None:
        pos = _Pos("sell", stop_loss=2010.0, take_profit=1990.0)
        bar = _bar(1985, 1988, 1980, 1986)
        assert _resolve_hard_boundary_exit(bar, pos) == (1985.0, "take_profit")
