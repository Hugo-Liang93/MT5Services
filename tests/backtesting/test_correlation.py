"""Tests for strategy signal correlation analysis."""

from __future__ import annotations

from src.backtesting.analysis.correlation import (
    CorrelationAnalysis,
    analyze_strategy_correlation,
    extract_signal_directions_from_evaluations,
)


def test_identical_strategies_perfect_correlation() -> None:
    """完全相同的方向序列应有相关性 1.0。"""
    directions = {
        "strategy_a": [1, -1, 1, 1, -1, 0, 1, -1, 1, 1] * 5,
        "strategy_b": [1, -1, 1, 1, -1, 0, 1, -1, 1, 1] * 5,
    }
    result = analyze_strategy_correlation(directions, min_overlap=10)
    assert len(result.pairs) == 1
    assert result.pairs[0].correlation > 0.99
    assert len(result.high_correlation_pairs) == 1
    # 其中一个应该被降权
    weights = result.strategy_weights
    assert min(weights.values()) == 0.50


def test_opposite_strategies_negative_correlation() -> None:
    """完全相反的方向序列应有相关性 -1.0。"""
    directions = {
        "trend": [1, -1, 1, 1, -1, 1, -1, 1, -1, 1] * 5,
        "reversal": [-1, 1, -1, -1, 1, -1, 1, -1, 1, -1] * 5,
    }
    result = analyze_strategy_correlation(directions, min_overlap=10)
    assert result.pairs[0].correlation < -0.99
    assert len(result.high_correlation_pairs) == 0
    # 反向策略不应被降权
    assert result.strategy_weights["trend"] == 1.0
    assert result.strategy_weights["reversal"] == 1.0


def test_independent_strategies_low_correlation() -> None:
    """混合方向的策略相关性应低于阈值，不触发降权。"""
    # a 偏多，b 方向混杂
    directions = {
        "a": [1, 1, -1, 1, 1, -1, 1, 1, -1, 1, 1, -1, 1, 1, -1,
              1, 1, -1, 1, 1, -1, 1, 1, -1, 1, 1, -1, 1, 1, -1],
        "b": [1, -1, 1, -1, 1, 1, -1, 1, -1, -1, 1, 1, -1, 1, -1,
              -1, 1, 1, -1, 1, 1, -1, -1, 1, -1, 1, -1, 1, 1, -1],
    }
    result = analyze_strategy_correlation(directions, min_overlap=10)
    assert abs(result.pairs[0].correlation) < 0.80
    assert result.strategy_weights["a"] == 1.0
    assert result.strategy_weights["b"] == 1.0


def test_single_strategy_returns_weight_1() -> None:
    directions = {"only": [1, -1, 1]}
    result = analyze_strategy_correlation(directions)
    assert result.strategy_weights == {"only": 1.0}
    assert len(result.pairs) == 0


def test_extract_from_evaluations() -> None:
    evals = [
        {"bar_time": "2026-01-01T00:00", "strategy": "a", "direction": "buy"},
        {"bar_time": "2026-01-01T00:00", "strategy": "b", "direction": "sell"},
        {"bar_time": "2026-01-01T01:00", "strategy": "a", "direction": "sell"},
        {"bar_time": "2026-01-01T01:00", "strategy": "b", "direction": "sell"},
    ]
    dirs = extract_signal_directions_from_evaluations(evals)
    assert dirs["a"] == [1, -1]
    assert dirs["b"] == [-1, -1]


def test_to_dict_structure() -> None:
    directions = {
        "x": [1, 1, 1, -1, 1] * 10,
        "y": [1, 1, 1, -1, 1] * 10,
    }
    result = analyze_strategy_correlation(directions, min_overlap=10)
    d = result.to_dict()
    assert "strategy_weights" in d
    assert "high_correlation_pairs" in d
    assert "total_bars_analyzed" in d
