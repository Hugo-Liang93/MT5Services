"""metrics.py 单元测试。"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from src.backtesting.metrics import (
    _compute_returns,
    _max_drawdown,
    _sharpe_ratio,
    _sortino_ratio,
    compute_metrics,
    compute_metrics_grouped,
)
from src.backtesting.models import TradeRecord


def _make_trade(
    pnl: float,
    strategy: str = "test",
    regime: str = "TRENDING",
    confidence: float = 0.7,
    bars_held: int = 5,
) -> TradeRecord:
    t = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return TradeRecord(
        signal_id="bt_test",
        strategy=strategy,
        direction="buy",
        entry_time=t,
        entry_price=2000.0,
        exit_time=t,
        exit_price=2000.0 + pnl,
        stop_loss=1990.0,
        take_profit=2010.0,
        position_size=0.1,
        pnl=pnl,
        pnl_pct=pnl / 100.0,
        bars_held=bars_held,
        regime=regime,
        confidence=confidence,
        exit_reason="take_profit" if pnl > 0 else "stop_loss",
    )


class TestComputeMetrics:
    def test_empty_trades(self) -> None:
        m = compute_metrics([], 10000.0, [])
        assert m.total_trades == 0
        assert m.win_rate == 0.0
        assert m.total_pnl == 0.0

    def test_all_winners(self) -> None:
        trades = [_make_trade(100.0), _make_trade(200.0), _make_trade(50.0)]
        equity = [10000.0, 10100.0, 10300.0, 10350.0]
        m = compute_metrics(trades, 10000.0, equity)
        assert m.total_trades == 3
        assert m.winning_trades == 3
        assert m.losing_trades == 0
        assert m.win_rate == 1.0
        assert m.total_pnl == 350.0
        assert m.profit_factor == 999.99

    def test_mixed_trades(self) -> None:
        trades = [
            _make_trade(100.0),
            _make_trade(-50.0),
            _make_trade(200.0),
            _make_trade(-80.0),
        ]
        equity = [10000.0, 10100.0, 10050.0, 10250.0, 10170.0]
        m = compute_metrics(trades, 10000.0, equity)
        assert m.total_trades == 4
        assert m.winning_trades == 2
        assert m.losing_trades == 2
        assert m.win_rate == 0.5
        assert m.total_pnl == 170.0
        assert m.profit_factor == pytest.approx(300.0 / 130.0, abs=0.01)

    def test_avg_bars_held(self) -> None:
        trades = [_make_trade(10, bars_held=3), _make_trade(20, bars_held=7)]
        m = compute_metrics(trades, 10000.0, [10000.0, 10010.0, 10030.0])
        assert m.avg_bars_held == 5.0


class TestComputeReturns:
    def test_basic(self) -> None:
        returns = _compute_returns([100.0, 110.0, 105.0])
        assert len(returns) == 2
        assert returns[0] == pytest.approx(0.1)
        assert returns[1] == pytest.approx(-0.04545, abs=0.001)

    def test_single_value(self) -> None:
        assert _compute_returns([100.0]) == []


class TestMaxDrawdown:
    def test_no_drawdown(self) -> None:
        dd, dur = _max_drawdown([100.0, 110.0, 120.0])
        assert dd == 0.0
        assert dur == 0

    def test_single_drawdown(self) -> None:
        dd, dur = _max_drawdown([100.0, 90.0, 80.0, 95.0])
        assert dd == pytest.approx(0.2)  # 20% drawdown
        assert dur == 2  # 2 bars from peak to trough

    def test_recovery(self) -> None:
        dd, dur = _max_drawdown([100.0, 80.0, 120.0, 90.0])
        # First dd: 20% (100→80), Second dd: 25% (120→90)
        assert dd == pytest.approx(0.25)


class TestSharpeRatio:
    def test_zero_std(self) -> None:
        assert _sharpe_ratio([0.01, 0.01, 0.01]) == 0.0

    def test_positive_returns(self) -> None:
        returns = [0.01, 0.02, 0.015, 0.01, 0.025]
        sr = _sharpe_ratio(returns)
        assert sr > 0  # 应该是正值


class TestSortinoRatio:
    def test_no_downside(self) -> None:
        result = _sortino_ratio([0.01, 0.02, 0.03])
        assert result == 999.99

    def test_with_downside(self) -> None:
        returns = [0.01, -0.02, 0.03, -0.01, 0.02]
        sr = _sortino_ratio(returns)
        assert isinstance(sr, float)


class TestCalmarRatio:
    def test_calmar_with_drawdown(self) -> None:
        """Calmar 应为年化收益率 / 最大回撤。"""
        trades = [_make_trade(100.0), _make_trade(-50.0), _make_trade(200.0)]
        equity = [10000.0, 10100.0, 10050.0, 10250.0]
        m = compute_metrics(trades, 10000.0, equity)
        assert m.calmar_ratio != 0.0
        # 年化因子 = 252 / 4 = 63
        # total_return_pct = 250 / 10000 = 0.025
        # annualized = 0.025 * 63 = 1.575
        # max_dd = (10100-10050)/10100 ≈ 0.00495
        # calmar = 1.575 / 0.00495 ≈ 318.18
        assert m.calmar_ratio > 0

    def test_calmar_zero_drawdown(self) -> None:
        """无回撤时 Calmar 应为 0。"""
        trades = [_make_trade(100.0)]
        equity = [10000.0, 10100.0]
        m = compute_metrics(trades, 10000.0, equity)
        assert m.calmar_ratio == 0.0


class TestGroupedMetrics:
    def test_by_strategy(self) -> None:
        trades = [
            _make_trade(100.0, strategy="rsi"),
            _make_trade(-50.0, strategy="rsi"),
            _make_trade(200.0, strategy="macd"),
        ]
        equity = [10000.0, 10100.0, 10050.0, 10250.0]
        grouped = compute_metrics_grouped(trades, 10000.0, equity, "strategy")
        assert "rsi" in grouped
        assert "macd" in grouped
        assert grouped["rsi"].total_trades == 2
        assert grouped["macd"].total_trades == 1

    def test_by_regime(self) -> None:
        trades = [
            _make_trade(100.0, regime="TRENDING"),
            _make_trade(-50.0, regime="RANGING"),
        ]
        grouped = compute_metrics_grouped(trades, 10000.0, [10000.0, 10100.0, 10050.0], "regime")
        assert "TRENDING" in grouped
        assert "RANGING" in grouped
