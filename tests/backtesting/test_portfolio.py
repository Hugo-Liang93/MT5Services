"""portfolio.py 单元测试。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pytest

from src.backtesting.engine.portfolio import PortfolioTracker
from src.clients.mt5_market import OHLC
from src.trading.execution import TradeParameters


def _bar(
    close: float,
    high: Optional[float] = None,
    low: Optional[float] = None,
    time_val: Optional[datetime] = None,
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

    def test_exit_slippage_buy(self) -> None:
        """多头出场应扣除滑点（实际出场价 = SL/TP - slippage）。"""
        pt = PortfolioTracker(
            initial_balance=10000.0, contract_size=100.0, slippage_points=0.5
        )
        bar = _bar(2000.0)
        params = _params(sl=1995.0, tp=2010.0, size=0.1)
        pt.open_position("test", "buy", bar, params, "TRENDING", 0.7, 0)

        # TP 触发
        exit_bar = _bar(2008.0, high=2012.0, low=2005.0)
        closed = pt.check_exits(exit_bar, 1)
        assert len(closed) == 1
        # 多头出场：exit_price = 2010.0 - 0.5 = 2009.5
        assert closed[0].exit_price == 2009.5

    def test_exit_slippage_sell(self) -> None:
        """空头出场应加滑点（实际出场价 = SL/TP + slippage）。"""
        pt = PortfolioTracker(
            initial_balance=10000.0, contract_size=100.0, slippage_points=0.5
        )
        bar = _bar(2000.0)
        params = _params(sl=2005.0, tp=1990.0, size=0.1)
        pt.open_position("test", "sell", bar, params, "TRENDING", 0.7, 0)

        # TP 触发
        exit_bar = _bar(1992.0, high=1998.0, low=1989.0)
        closed = pt.check_exits(exit_bar, 1)
        assert len(closed) == 1
        # 空头出场：exit_price = 1990.0 + 0.5 = 1990.5
        assert closed[0].exit_price == 1990.5

    def test_pnl_pct_uses_initial_balance(self) -> None:
        """PnL 百分比应使用初始资金作为分母。"""
        pt = PortfolioTracker(initial_balance=10000.0, contract_size=100.0)
        bar = _bar(2000.0)
        params = _params(sl=1995.0, tp=2010.0, size=0.1)
        pt.open_position("test", "buy", bar, params, "TRENDING", 0.7, 0)

        exit_bar = _bar(2008.0, high=2012.0, low=2005.0)
        closed = pt.check_exits(exit_bar, 1)
        assert len(closed) == 1
        # PnL% = pnl / initial_balance * 100 (not current_balance)
        expected_pnl_pct = closed[0].pnl / 10000.0 * 100.0
        assert closed[0].pnl_pct == pytest.approx(round(expected_pnl_pct, 4))

    def test_close_by_signal(self) -> None:
        """按策略名关闭持仓。"""
        pt = PortfolioTracker(initial_balance=10000.0, max_positions=5)
        bar = _bar(2000.0)
        params = _params()
        pt.open_position("s1", "buy", bar, params, "TRENDING", 0.7, 0)
        pt.open_position("s2", "sell", bar, params, "RANGING", 0.6, 0)

        closed = pt.close_by_signal("s1", _bar(2005.0), 5)
        assert len(closed) == 1
        assert closed[0].strategy == "s1"
        assert pt.open_position_count == 1

    def test_max_volume_per_order(self) -> None:
        pt = PortfolioTracker(initial_balance=10000.0, max_volume_per_order=0.05)
        bar = _bar(2000.0)
        params = _params(size=0.10)
        assert pt.open_position("test", "buy", bar, params, "TRENDING", 0.7, 0) is False

    def test_max_trades_per_day(self) -> None:
        pt = PortfolioTracker(initial_balance=10000.0, max_trades_per_day=1)
        bar1 = _bar(2000.0, time_val=datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc))
        bar2 = _bar(2001.0, time_val=datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc))
        params = _params(size=0.05)
        assert pt.open_position("s1", "buy", bar1, params, "TRENDING", 0.7, 0) is True
        assert pt.open_position("s2", "buy", bar2, params, "TRENDING", 0.7, 1) is False

    def test_max_trades_per_hour(self) -> None:
        pt = PortfolioTracker(initial_balance=10000.0, max_trades_per_hour=1)
        bar1 = _bar(2000.0, time_val=datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc))
        bar2 = _bar(2001.0, time_val=datetime(2025, 1, 1, 9, 30, tzinfo=timezone.utc))
        params = _params(size=0.05)
        assert pt.open_position("s1", "buy", bar1, params, "TRENDING", 0.7, 0) is True
        assert pt.open_position("s2", "buy", bar2, params, "TRENDING", 0.7, 1) is False

    def test_max_volume_per_day(self) -> None:
        pt = PortfolioTracker(initial_balance=10000.0, max_volume_per_day=0.15)
        bar1 = _bar(2000.0, time_val=datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc))
        bar2 = _bar(2001.0, time_val=datetime(2025, 1, 1, 11, 0, tzinfo=timezone.utc))
        params = _params(size=0.10)
        assert pt.open_position("s1", "buy", bar1, params, "TRENDING", 0.7, 0) is True
        assert pt.open_position("s2", "buy", bar2, params, "TRENDING", 0.7, 1) is False

    def test_daily_loss_limit_blocks_new_entries(self) -> None:
        pt = PortfolioTracker(
            initial_balance=10000.0,
            contract_size=100.0,
            daily_loss_limit_pct=1.0,
        )
        day_start = _bar(2000.0, time_val=datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc))
        pt.observe_bar(day_start)
        params = _params(sl=1995.0, tp=2010.0, size=1.0)
        assert pt.open_position("s1", "buy", day_start, params, "TRENDING", 0.7, 0) is True

        # 先制造较大浮亏，使当日亏损超过 1%
        stressed = _bar(1985.0, high=1988.0, low=1984.0, time_val=datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc))
        pt.record_equity(stressed)

        second_params = _params(entry=1985.0, sl=1980.0, tp=1995.0, size=0.10)
        assert pt.open_position("s2", "buy", stressed, second_params, "TRENDING", 0.7, 1) is False
