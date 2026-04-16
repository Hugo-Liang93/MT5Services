"""tests/research/features/test_config.py — FeatureProviderConfig 配置测试。"""

from __future__ import annotations

import pytest

from src.research.core.config import (
    CrossTFProviderConfig,
    FeatureProviderConfig,
    MicrostructureProviderConfig,
    RegimeTransitionProviderConfig,
    ResearchConfig,
    TemporalProviderConfig,
    load_research_config,
)


# ---------------------------------------------------------------------------
# TemporalProviderConfig 默认值
# ---------------------------------------------------------------------------


class TestTemporalProviderConfig:
    def test_default_core_indicators(self) -> None:
        cfg = TemporalProviderConfig()
        assert cfg.core_indicators == ["rsi14", "adx14"]

    def test_default_aux_indicators(self) -> None:
        cfg = TemporalProviderConfig()
        assert cfg.aux_indicators == ["macd_histogram", "cci20", "roc12", "stoch_k"]

    def test_default_windows(self) -> None:
        cfg = TemporalProviderConfig()
        assert cfg.windows == [3, 5, 10]

    def test_default_cross_levels_rsi(self) -> None:
        cfg = TemporalProviderConfig()
        assert cfg.cross_levels_rsi == [30.0, 50.0, 70.0]

    def test_default_cross_levels_adx(self) -> None:
        cfg = TemporalProviderConfig()
        assert cfg.cross_levels_adx == [20.0, 25.0]

    def test_frozen(self) -> None:
        cfg = TemporalProviderConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.windows = [1, 2, 3]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# MicrostructureProviderConfig 默认值
# ---------------------------------------------------------------------------


class TestMicrostructureProviderConfig:
    def test_default_lookback(self) -> None:
        cfg = MicrostructureProviderConfig()
        assert cfg.lookback == 5

    def test_frozen(self) -> None:
        cfg = MicrostructureProviderConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.lookback = 10  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CrossTFProviderConfig 默认值
# ---------------------------------------------------------------------------


class TestCrossTFProviderConfig:
    def test_default_parent_tf_map(self) -> None:
        cfg = CrossTFProviderConfig()
        assert cfg.parent_tf_map == {"M15": "H1", "M30": "H4", "H1": "H4", "H4": "D1"}

    def test_default_parent_indicators(self) -> None:
        cfg = CrossTFProviderConfig()
        assert cfg.parent_indicators == [
            "supertrend_direction",
            "rsi14",
            "adx14",
            "ema50",
            "bb_position",
        ]

    def test_frozen(self) -> None:
        cfg = CrossTFProviderConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.parent_tf_map = {}  # type: ignore[misc]


# ---------------------------------------------------------------------------
# RegimeTransitionProviderConfig 默认值
# ---------------------------------------------------------------------------


class TestRegimeTransitionProviderConfig:
    def test_default_history_window(self) -> None:
        cfg = RegimeTransitionProviderConfig()
        assert cfg.history_window == 50

    def test_default_prob_delta_window(self) -> None:
        cfg = RegimeTransitionProviderConfig()
        assert cfg.prob_delta_window == 5

    def test_frozen(self) -> None:
        cfg = RegimeTransitionProviderConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.history_window = 100  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FeatureProviderConfig 启用开关与 is_enabled()
# ---------------------------------------------------------------------------


class TestFeatureProviderConfig:
    def test_temporal_enabled_by_default(self) -> None:
        cfg = FeatureProviderConfig()
        assert cfg.temporal_enabled is True

    def test_microstructure_enabled_by_default(self) -> None:
        cfg = FeatureProviderConfig()
        assert cfg.microstructure_enabled is True

    def test_cross_tf_disabled_by_default(self) -> None:
        """cross_tf 默认关闭（需额外加载父 TF 数据）。"""
        cfg = FeatureProviderConfig()
        assert cfg.cross_tf_enabled is False

    def test_regime_transition_enabled_by_default(self) -> None:
        cfg = FeatureProviderConfig()
        assert cfg.regime_transition_enabled is True

    def test_session_event_enabled_by_default(self) -> None:
        cfg = FeatureProviderConfig()
        assert cfg.session_event_enabled is True

    def test_intrabar_enabled_by_default(self) -> None:
        cfg = FeatureProviderConfig()
        assert cfg.intrabar_enabled is True

    def test_default_fdr_grouping(self) -> None:
        cfg = FeatureProviderConfig()
        assert cfg.fdr_grouping == "by_provider"

    # --- is_enabled() ---

    def test_is_enabled_temporal(self) -> None:
        cfg = FeatureProviderConfig()
        assert cfg.is_enabled("temporal") is True

    def test_is_enabled_cross_tf_false(self) -> None:
        cfg = FeatureProviderConfig()
        assert cfg.is_enabled("cross_tf") is False

    def test_is_enabled_microstructure(self) -> None:
        cfg = FeatureProviderConfig()
        assert cfg.is_enabled("microstructure") is True

    def test_is_enabled_regime_transition(self) -> None:
        cfg = FeatureProviderConfig()
        assert cfg.is_enabled("regime_transition") is True

    def test_is_enabled_session_event(self) -> None:
        cfg = FeatureProviderConfig()
        assert cfg.is_enabled("session_event") is True

    def test_is_enabled_intrabar(self) -> None:
        cfg = FeatureProviderConfig()
        assert cfg.is_enabled("intrabar") is True

    def test_is_enabled_unknown_returns_true(self) -> None:
        """未知 provider 名默认返回 True（不阻止加载）。"""
        cfg = FeatureProviderConfig()
        assert cfg.is_enabled("nonexistent_provider") is True

    def test_is_enabled_respects_disabled_flag(self) -> None:
        cfg = FeatureProviderConfig(cross_tf_enabled=False)
        assert cfg.is_enabled("cross_tf") is False

    def test_is_enabled_when_explicitly_enabled(self) -> None:
        cfg = FeatureProviderConfig(cross_tf_enabled=True)
        assert cfg.is_enabled("cross_tf") is True

    # --- 子配置默认实例 ---

    def test_default_temporal_sub_config(self) -> None:
        cfg = FeatureProviderConfig()
        assert isinstance(cfg.temporal, TemporalProviderConfig)
        assert cfg.temporal.core_indicators == ["rsi14", "adx14"]

    def test_default_microstructure_sub_config(self) -> None:
        cfg = FeatureProviderConfig()
        assert isinstance(cfg.microstructure, MicrostructureProviderConfig)
        assert cfg.microstructure.lookback == 5

    def test_default_cross_tf_sub_config(self) -> None:
        cfg = FeatureProviderConfig()
        assert isinstance(cfg.cross_tf, CrossTFProviderConfig)

    def test_default_regime_transition_sub_config(self) -> None:
        cfg = FeatureProviderConfig()
        assert isinstance(cfg.regime_transition, RegimeTransitionProviderConfig)

    def test_frozen(self) -> None:
        cfg = FeatureProviderConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.temporal_enabled = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ResearchConfig 集成：包含 feature_providers 字段
# ---------------------------------------------------------------------------


class TestResearchConfigIntegration:
    def test_research_config_has_feature_providers(self) -> None:
        cfg = ResearchConfig()
        assert hasattr(cfg, "feature_providers")
        assert isinstance(cfg.feature_providers, FeatureProviderConfig)

    def test_research_config_still_has_feature_engineering(self) -> None:
        """向后兼容：feature_engineering 字段保留（Task 13 再移除）。"""
        from src.research.core.config import FeatureEngineeringConfig

        cfg = ResearchConfig()
        assert hasattr(cfg, "feature_engineering")
        assert isinstance(cfg.feature_engineering, FeatureEngineeringConfig)


# ---------------------------------------------------------------------------
# load_research_config() — INI 加载
# ---------------------------------------------------------------------------


class TestLoadResearchConfig:
    def test_returns_research_config(self) -> None:
        cfg = load_research_config()
        assert isinstance(cfg, ResearchConfig)

    def test_feature_providers_field_present(self) -> None:
        cfg = load_research_config()
        assert isinstance(cfg.feature_providers, FeatureProviderConfig)

    def test_cross_tf_disabled_from_ini(self) -> None:
        """INI 中 cross_tf = false → cross_tf_enabled = False。"""
        cfg = load_research_config()
        assert cfg.feature_providers.cross_tf_enabled is False

    def test_temporal_enabled_from_ini(self) -> None:
        cfg = load_research_config()
        assert cfg.feature_providers.temporal_enabled is True

    def test_fdr_grouping_from_ini(self) -> None:
        cfg = load_research_config()
        assert cfg.feature_providers.fdr_grouping == "by_provider"

    def test_temporal_core_indicators_from_ini(self) -> None:
        cfg = load_research_config()
        assert cfg.feature_providers.temporal.core_indicators == ["rsi14", "adx14"]

    def test_temporal_windows_from_ini(self) -> None:
        cfg = load_research_config()
        assert cfg.feature_providers.temporal.windows == [3, 5, 10]

    def test_microstructure_lookback_from_ini(self) -> None:
        cfg = load_research_config()
        assert cfg.feature_providers.microstructure.lookback == 5

    def test_cross_tf_parent_tf_map_from_ini(self) -> None:
        cfg = load_research_config()
        assert cfg.feature_providers.cross_tf.parent_tf_map == {
            "M15": "H1",
            "M30": "H4",
            "H1": "H4",
            "H4": "D1",
        }

    def test_regime_transition_history_window_from_ini(self) -> None:
        cfg = load_research_config()
        assert cfg.feature_providers.regime_transition.history_window == 50

    def test_regime_transition_prob_delta_window_from_ini(self) -> None:
        cfg = load_research_config()
        assert cfg.feature_providers.regime_transition.prob_delta_window == 5
