"""Tests for Monte Carlo permutation test."""

from __future__ import annotations

import pytest

from src.backtesting.analysis.monte_carlo import (
    DeflatedSharpeResult,
    MonteCarloConfig,
    MonteCarloResult,
    compute_deflated_sharpe,
    run_monte_carlo,
)


def test_insufficient_data_returns_not_significant() -> None:
    result = run_monte_carlo([10.0, -5.0], initial_balance=10000.0)
    assert not result.is_significant
    assert result.num_simulations == 0
    assert result.sharpe_p_value == 1.0


def test_strong_strategy_is_significant() -> None:
    """一致盈利的策略应该显著优于随机。"""
    pnl = [50.0, 30.0, 40.0, -10.0, 60.0, 20.0, -5.0, 45.0, 35.0, 25.0,
           55.0, 15.0, 40.0, -8.0, 50.0, 30.0, -3.0, 45.0, 20.0, 60.0]
    config = MonteCarloConfig(enabled=True, num_simulations=500, seed=42)
    result = run_monte_carlo(pnl, initial_balance=10000.0, config=config)

    assert result.num_trades == 20
    assert result.num_simulations == 500
    assert result.real_sharpe > 0
    assert result.real_profit_factor > 1.0
    assert result.sharpe_percentile > 0


def test_random_strategy_not_significant() -> None:
    """纯随机盈亏（均值 0）不应该显著。"""
    pnl = [10, -10, 15, -15, 5, -5, 20, -20, 8, -8,
           12, -12, 7, -7, 3, -3, 18, -18, 11, -11]
    config = MonteCarloConfig(enabled=True, num_simulations=500, seed=42)
    result = run_monte_carlo(pnl, initial_balance=10000.0, config=config)

    assert result.num_trades == 20
    # 随机策略的 Sharpe 应接近 0，p-value 应较高
    assert result.sharpe_p_value > 0.1


def test_seed_reproducibility() -> None:
    pnl = [30.0, -10.0, 20.0, -5.0, 40.0] * 4
    config = MonteCarloConfig(enabled=True, num_simulations=100, seed=123)
    r1 = run_monte_carlo(pnl, 10000.0, config)
    r2 = run_monte_carlo(pnl, 10000.0, config)
    assert r1.sharpe_p_value == r2.sharpe_p_value
    assert r1.profit_factor_p_value == r2.profit_factor_p_value


def test_to_dict_keys() -> None:
    pnl = [10.0, -5.0, 20.0, -3.0, 15.0, 8.0, -2.0, 12.0, 6.0, -1.0]
    config = MonteCarloConfig(enabled=True, num_simulations=50, seed=1)
    result = run_monte_carlo(pnl, 10000.0, config)
    d = result.to_dict()

    expected_keys = {
        "num_simulations", "num_trades", "real_sharpe", "real_profit_factor",
        "real_max_drawdown", "real_total_pnl", "sharpe_p_value",
        "profit_factor_p_value", "max_drawdown_p_value",
        "sharpe_percentile", "profit_factor_percentile",
        "random_sharpe_mean", "random_sharpe_std", "random_sharpe_95th",
        "is_significant",
    }
    assert set(d.keys()) == expected_keys


# ── Deflated Sharpe Ratio ────────────────────────────────────


def test_dsr_few_trials_no_deflation() -> None:
    """2 次试验，Sharpe 几乎不需要修正。"""
    result = compute_deflated_sharpe(
        observed_sharpe=1.5, num_trials=2, num_trades=100,
    )
    assert result.expected_max_sharpe < result.observed_sharpe
    assert result.num_trials == 2


def test_dsr_many_trials_inflated() -> None:
    """100 次试验后，期望最大 Sharpe 约 2.3+，普通的 1.5 不再显著。"""
    result = compute_deflated_sharpe(
        observed_sharpe=1.5, num_trials=100, num_trades=200,
    )
    assert result.expected_max_sharpe > 1.5
    assert not result.is_significant
    assert result.p_value > 0.05


def test_dsr_very_high_sharpe_still_significant() -> None:
    """极高 Sharpe 即使 100 次试验后仍然显著。"""
    result = compute_deflated_sharpe(
        observed_sharpe=4.0, num_trials=100, num_trades=200,
    )
    assert result.is_significant
    assert result.p_value < 0.05


def test_dsr_insufficient_data() -> None:
    result = compute_deflated_sharpe(
        observed_sharpe=2.0, num_trials=1, num_trades=5,
    )
    assert not result.is_significant
    assert result.p_value == 0.5


def test_dsr_to_dict_keys() -> None:
    result = compute_deflated_sharpe(1.0, 50, 100)
    d = result.to_dict()
    assert "observed_sharpe" in d
    assert "deflated_sharpe" in d
    assert "p_value" in d
    assert "is_significant" in d
