"""CandlePatternFeatureProvider 测试。

把已注册的 `candle_pattern` indicator 的 directional 输出（hammer / engulfing /
pin_bar / rejection / three_method 各 1/-1/0）拆成 binary mining feature，
让 mining 体系（predictive_power / barrier_predictive_power / rule_mining）
能为每个 K 线形态独立评估 edge——这正是 pa 策略人工选用的形态来源。
"""

from __future__ import annotations

from types import SimpleNamespace

from src.research.features.candle_patterns import CandlePatternFeatureProvider
from src.research.features.protocol import FeatureProvider, FeatureRole


def _make_matrix(*, n_bars: int, candle_series: dict) -> SimpleNamespace:
    """最小 matrix-like：仅暴露 n_bars + indicator_series.get()。"""
    return SimpleNamespace(
        n_bars=n_bars,
        indicator_series=candle_series,
    )


def test_provider_satisfies_protocol() -> None:
    provider = CandlePatternFeatureProvider()
    assert isinstance(provider, FeatureProvider)
    assert provider.name == "candle_patterns"


def test_feature_count_matches_role_mapping() -> None:
    provider = CandlePatternFeatureProvider()
    role_mapping = provider.role_mapping()
    assert provider.feature_count == len(role_mapping)


def test_required_columns_list_candle_pattern_fields() -> None:
    provider = CandlePatternFeatureProvider()
    cols = provider.required_columns()
    expected_indicators = {
        "hammer",
        "engulfing",
        "doji",
        "pin_bar",
        "inside_bar",
        "three_method",
        "rejection",
        "consecutive_dir",
    }
    actual = {field for (ind, field) in cols if ind == "candle_pattern"}
    assert expected_indicators.issubset(actual)


def test_directional_split_to_binary_features() -> None:
    """hammer = [+1, 0, -1, 0, +1] →
    hammer_bullish = [1.0, 0.0, 0.0, 0.0, 1.0]
    hammer_bearish = [0.0, 0.0, 1.0, 0.0, 0.0]
    """
    provider = CandlePatternFeatureProvider()
    candle_series = {
        ("candle_pattern", "hammer"): [1.0, 0.0, -1.0, 0.0, 1.0],
        ("candle_pattern", "engulfing"): [0.0, 0.0, 0.0, 0.0, 0.0],
        ("candle_pattern", "doji"): [0.0, 1.0, 0.0, 0.0, 0.0],
        ("candle_pattern", "pin_bar"): [0.0, 0.0, 0.0, 0.0, 0.0],
        ("candle_pattern", "inside_bar"): [0.0, 0.0, 0.0, 1.0, 0.0],
        ("candle_pattern", "three_method"): [0.0, 0.0, 0.0, 0.0, 0.0],
        ("candle_pattern", "rejection"): [0.0, 0.0, 0.0, 0.0, 0.0],
        ("candle_pattern", "consecutive_dir"): [1.0, 2.0, -1.0, -2.0, 1.0],
    }
    matrix = _make_matrix(n_bars=5, candle_series=candle_series)
    result = provider.compute(matrix)

    assert result[("candle_patterns", "hammer_bullish")] == [1.0, 0.0, 0.0, 0.0, 1.0]
    assert result[("candle_patterns", "hammer_bearish")] == [0.0, 0.0, 1.0, 0.0, 0.0]
    assert result[("candle_patterns", "doji")] == [0.0, 1.0, 0.0, 0.0, 0.0]
    assert result[("candle_patterns", "inside_bar")] == [0.0, 0.0, 0.0, 1.0, 0.0]
    # consecutive_dir 是 signed，不拆方向，原值保留
    assert result[("candle_patterns", "consecutive_dir")] == [1.0, 2.0, -1.0, -2.0, 1.0]


def test_three_method_splits_to_soldiers_crows() -> None:
    """three_method = [+1, 0, -1] → three_soldiers / three_crows 二分。"""
    provider = CandlePatternFeatureProvider()
    base = {
        ("candle_pattern", k): [0.0, 0.0, 0.0]
        for k in [
            "hammer",
            "engulfing",
            "doji",
            "pin_bar",
            "inside_bar",
            "rejection",
            "consecutive_dir",
        ]
    }
    base[("candle_pattern", "three_method")] = [1.0, 0.0, -1.0]
    matrix = _make_matrix(n_bars=3, candle_series=base)
    result = provider.compute(matrix)

    assert result[("candle_patterns", "three_soldiers")] == [1.0, 0.0, 0.0]
    assert result[("candle_patterns", "three_crows")] == [0.0, 0.0, 1.0]


def test_compute_when_candle_pattern_missing_returns_zeros() -> None:
    """indicator 未计算（早期 bar warmup）→ 全 0 而不是 raise。"""
    provider = CandlePatternFeatureProvider()
    matrix = _make_matrix(n_bars=3, candle_series={})  # 完全空
    result = provider.compute(matrix)

    assert result[("candle_patterns", "hammer_bullish")] == [0.0, 0.0, 0.0]
    assert result[("candle_patterns", "doji")] == [0.0, 0.0, 0.0]
    assert result[("candle_patterns", "consecutive_dir")] == [0.0, 0.0, 0.0]


def test_empty_matrix_returns_empty_lists() -> None:
    """n_bars=0 → 所有特征空列表，对齐其他 provider 行为。"""
    provider = CandlePatternFeatureProvider()
    matrix = _make_matrix(n_bars=0, candle_series={})
    result = provider.compute(matrix)
    for values in result.values():
        assert values == []


def test_role_mapping_assigns_when_for_all_patterns() -> None:
    """所有 binary K 线形态默认 role=WHEN（入场触发），consecutive_dir → WHY（趋势确认）。"""
    provider = CandlePatternFeatureProvider()
    roles = provider.role_mapping()
    when_features = {
        "hammer_bullish",
        "hammer_bearish",
        "engulfing_bullish",
        "engulfing_bearish",
        "pin_bar_bullish",
        "pin_bar_bearish",
        "rejection_bullish",
        "rejection_bearish",
        "doji",
        "inside_bar",
        "three_soldiers",
        "three_crows",
    }
    for name in when_features:
        assert roles[name] == FeatureRole.WHEN
    assert roles["consecutive_dir"] == FeatureRole.WHY


def test_provider_no_extra_data_required() -> None:
    """不需要跨 TF 数据。"""
    provider = CandlePatternFeatureProvider()
    assert provider.required_extra_data() is None
