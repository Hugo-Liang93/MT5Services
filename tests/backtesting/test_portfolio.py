"""portfolio.py 单元测试。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.backtesting.portfolio import PortfolioTracker
from src.clients.mt5_market import OHLC
from src.trading.sizing import TradeParameters


def _bar(
    close: float,
    high: float | None = None,
    low: float | None = None,
    time_val: datetime | None = None,
) -> OHLC:
    t = time_val or datetime(2025, 1, 1, tzinfo=timezone.utc)
    h = high if high is not None else close + 1.0
    lo = low if low is not None else close - 1.0
    return OHLC(
        symbol="XAUUSD",
        timeframe="M5",
        time=t,
        open=close,
        high=h,
        low=lo,
        close=close,
        volume=100.0,
    )


def _params(
    entry: float = 2000.0,
    sl: float = 1995.0,
    tp: float = 2010.0,
    size: float = 0.1,
) -> TradeParameters:
    return TradeParameters(
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        position_size=size,
        risk_reward_ratio=2.0,
        atr_value=5.0,
        sl_distance=5.0,
        tp_distance=10.0,
    )


class TestPortfolioTracker:
    def test_initial_state(self) -> None:
        pt = PortfolioTracker(initial_balance=10000.0)
        assert pt.current_balance == 10000.0
        assert pt.open_position_count == 0
        assert pt.closed_trades == []

    def test_open_position(self) -> None:
        pt = PortfolioTracker(initial_balance=10000.0)
        bar = _bar(2000.0)
        params = _params()
        ok = pt.open_position("test", "buy", bar, params, "TRENDING", 0.7, 0)
        assert ok is True
        assert pt.open_position_count == 1

    def test_max_positions(self) -> None:
        pt = PortfolioTracker(initial_balance=10000.0, max_positions=1)
        bar = _bar(2000.0)
        params = _params()
        assert pt.open_position("s1", "buy", bar, params, "TRENDING", 0.7, 0)
        assert not pt.open_position("s2", "buy", bar, params, "TRENDING", 0.7, 1)

    def test_stop_loss_buy(self) -> None:
        pt = PortfolioTracker(initial_balance=10000.0, contract_size=100.0)
        bar = _bar(2000.0)
        params = _params(sl=1995.0, tp=2010.0, size=0.1)
        pt.open_position("test", "buy", bar, params, "TRENDING", 0.7, 0)

        # SL 触发：bar low <= 1995
        exit_bar = _bar(1998.0, high=2002.0, low=1994.0)
        closed = pt.check_exits(exit_bar, 1)
        assert len(closed) == 1
        assert closed[0].exit_reason == "stop_loss"
        assert closed[0].exit_price == 1995.0
        assert closed[0].pnl < 0

    def test_take_profit_buy(self) -> None:
        pt = PortfolioTracker(initial_balance=10000.0, contract_size=100.0)
        bar = _bar(2000.0)
        params = _params(sl=1995.0, tp=2010.0, size=0.1)
        pt.open_position("test", "buy", bar, params, "TRENDING", 0.7, 0)

        # TP 触发：bar high >= 2010
        exit_bar = _bar(2008.0, high=2012.0, low=2005.0)
        closed = pt.check_exits(exit_bar, 1)
        assert len(closed) == 1
        assert closed[0].exit_reason == "take_profit"
        assert closed[0].exit_price == 2010.0
        assert closed[0].pnl > 0

    def test_stop_loss_sell(self) -> None:
        pt = PortfolioTracker(initial_balance=10000.0, contract_size=100.0)
        bar = _bar(2000.0)
        params = _params(sl=2005.0, tp=1990.0, size=0.1)
        pt.open_position("test", "sell", bar, params, "TRENDING", 0.7, 0)

        # SL 触发：bar high >= 2005
        exit_bar = _bar(2003.0, high=2006.0, low=2001.0)
        closed = pt.check_exits(exit_bar, 1)
        assert len(closed) == 1
        assert closed[0].exit_reason == "stop_loss"
        assert closed[0].pnl < 0

    def test_close_all(self) -> None:
        pt = PortfolioTracker(initial_balance=10000.0, max_positions=5)
        bar = _bar(2000.0)
        params = _params()
        pt.open_position("s1", "buy", bar, params, "TRENDING", 0.7, 0)
        pt.open_position("s2", "sell", bar, params, "RANGING", 0.6, 0)

        final_bar = _bar(2005.0)
        closed = pt.close_all(final_bar, 10)
        assert len(closed) == 2
        assert pt.open_position_count == 0

    def test_equity_curve(self) -> None:
        pt = PortfolioTracker(initial_balance=10000.0)
        bar1 = _bar(2000.0, time_val=datetime(2025, 1, 1, tzinfo=timezone.utc))
        bar2 = _bar(2005.0, time_val=datetime(2025, 1, 2, tzinfo=timezone.utc))
        pt.record_equity(bar1)
        pt.record_equity(bar2)
        curve = pt.equity_curve
        assert len(curve) == 2
        assert curve[0][1] == 10000.0  # 无持仓时资金不变

    def test_bars_held(self) -> None:
        pt = PortfolioTracker(initial_balance=10000.0, contract_size=100.0)
        bar = _bar(2000.0)
        params = _params(sl=1990.0, tp=2020.0, size=0.1)
        pt.open_position("test", "buy", bar, params, "TRENDING", 0.7, bar_index=5)

        exit_bar = _bar(2000.0, high=2025.0, low=1999.0)
        closed = pt.check_exits(exit_bar, bar_index=15)
        assert len(closed) == 1
        assert closed[0].bars_held == 10

    def test_slippage(self) -> None:
        pt = PortfolioTracker(initial_balance=10000.0, slippage_points=0.5)
        bar = _bar(2000.0)
        params = _params()
        pt.open_position("test", "buy", bar, params, "TRENDING", 0.7, 0)
        # 多头：entry_price = close + slippage = 2000.5
        pos = pt._open_positions[0]
        assert pos.entry_price == 2000.5
