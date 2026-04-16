"""tests/research/features/test_hub.py

FeatureHub 单元测试 + 集成测试。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, cast
from unittest.mock import MagicMock, patch

import pytest

from src.research.features.protocol import (
    FeatureComputeResult,
    FeatureRole,
    ProviderDataRequirement,
)


# ---------------------------------------------------------------------------
# Mock DataMatrix helpers
# ---------------------------------------------------------------------------


def _make_matrix(n: int = 10) -> Any:
    """构造最小化 DataMatrix mock。"""
    m = MagicMock()
    m.n_bars = n
    m.indicator_series = {}
    return m


def _make_mock_provider(
    name: str,
    features: Dict[Tuple[str, str], List[Optional[float]]],
    role_map: Dict[str, FeatureRole],
    extra_req: Optional[ProviderDataRequirement] = None,
) -> Any:
    """构造满足 FeatureProvider Protocol 的 Mock 对象。"""
    provider = MagicMock()
    provider.name = name
    provider.feature_count = len(features)
    provider.required_columns.return_value = []
    provider.required_extra_data.return_value = extra_req
    provider.role_mapping.return_value = role_map
    provider.compute.return_value = features
    return provider


# ---------------------------------------------------------------------------
# 单元测试：Mock Provider
# ---------------------------------------------------------------------------


class TestRegisterAndCompute:
    """test_register_and_compute: mock provider 注册与特征注入验证。"""

    def test_register_and_compute(self) -> None:
        """mock provider 返回特征 → 验证注入到 matrix.indicator_series。"""
        from src.research.features.hub import FeatureHub
        from src.research.core.config import ResearchConfig

        n = 10
        vals: List[Optional[float]] = [1.0] * n
        features: Dict[Tuple[str, str], List[Optional[float]]] = {("mock_ind", "val"): vals}
        role_map = {"val": FeatureRole.WHY}
        provider = _make_mock_provider("mock_p", features, role_map)

        config = ResearchConfig()
        hub = FeatureHub.__new__(FeatureHub)
        hub._providers = {"mock_p": provider}
        hub._computed_keys = cast(Dict[str, List[Tuple[str, str]]], {})

        matrix = _make_matrix(n)
        result = hub.compute_all(matrix)

        assert ("mock_ind", "val") in matrix.indicator_series
        assert matrix.indicator_series[("mock_ind", "val")] == [1.0] * n
        assert isinstance(result, FeatureComputeResult)
        assert result.total_features == 1

    def test_compute_all_passes_extra_data(self) -> None:
        """compute_all 将 extra_data 传入 provider.compute。"""
        from src.research.features.hub import FeatureHub

        n = 5
        features: Dict[Tuple[str, str], List[Optional[float]]] = {
            ("ind", "f"): [0.5] * n
        }
        provider = _make_mock_provider("p", features, {"f": FeatureRole.WHEN})

        hub = FeatureHub.__new__(FeatureHub)
        hub._providers = {"p": provider}
        hub._computed_keys = {}

        matrix = _make_matrix(n)
        extra = {"parent_data": [1, 2, 3]}
        hub.compute_all(matrix, extra_data=extra)

        provider.compute.assert_called_once_with(matrix, extra)


class TestFeatureNamesByProvider:
    """test_feature_names_by_provider: compute 后按 provider 分组验证。"""

    def test_feature_names_by_provider(self) -> None:
        """compute 后 feature_names_by_provider 返回正确分组。"""
        from src.research.features.hub import FeatureHub

        n = 5
        feat_a: Dict[Tuple[str, str], List[Optional[float]]] = {
            ("ind_a", "x"): [1.0] * n,
            ("ind_a", "y"): [2.0] * n,
        }
        feat_b: Dict[Tuple[str, str], List[Optional[float]]] = {("ind_b", "z"): [3.0] * n}

        pa = _make_mock_provider("provider_a", feat_a, {})
        pb = _make_mock_provider("provider_b", feat_b, {})

        hub = FeatureHub.__new__(FeatureHub)
        hub._providers = {"provider_a": pa, "provider_b": pb}
        hub._computed_keys = {}

        matrix = _make_matrix(n)
        hub.compute_all(matrix)

        groups = hub.feature_names_by_provider()
        assert set(groups["provider_a"]) == {("ind_a", "x"), ("ind_a", "y")}
        assert groups["provider_b"] == [("ind_b", "z")]

    def test_feature_names_empty_before_compute(self) -> None:
        """compute 前 feature_names_by_provider 返回空字典。"""
        from src.research.features.hub import FeatureHub

        hub = FeatureHub.__new__(FeatureHub)
        hub._providers = {}
        hub._computed_keys = {}

        groups = hub.feature_names_by_provider()
        assert groups == {}


class TestRequiredExtraDataAggregation:
    """test_required_extra_data_aggregation: 聚合多个 provider 的额外数据需求。"""

    def test_one_provider_needs_extra_data(self) -> None:
        """一个 provider 声明需要额外数据 → 聚合后包含该需求。"""
        from src.research.features.hub import FeatureHub

        req = ProviderDataRequirement(
            parent_tf_mapping={"M15": "H1"},
            parent_indicators=["rsi14"],
        )
        pa = _make_mock_provider("a", {}, {}, extra_req=req)
        pb = _make_mock_provider("b", {}, {}, extra_req=None)

        hub = FeatureHub.__new__(FeatureHub)
        hub._providers = {"a": pa, "b": pb}
        hub._computed_keys = {}

        reqs = hub.required_extra_data()
        assert len(reqs) == 1
        assert reqs[0] is req

    def test_no_provider_needs_extra_data(self) -> None:
        """所有 provider 均无额外数据需求 → 返回空列表。"""
        from src.research.features.hub import FeatureHub

        pa = _make_mock_provider("a", {}, {}, extra_req=None)

        hub = FeatureHub.__new__(FeatureHub)
        hub._providers = {"a": pa}
        hub._computed_keys = {}

        assert hub.required_extra_data() == []


class TestRoleMappingAll:
    """test_role_mapping_all: 聚合所有 provider 的角色映射。"""

    def test_role_mapping_all(self) -> None:
        """合并多个 provider 的 role_mapping 结果。"""
        from src.research.features.hub import FeatureHub

        role_a = {"feature_x": FeatureRole.WHY}
        role_b = {"feature_y": FeatureRole.WHEN, "feature_z": FeatureRole.WHERE}

        pa = _make_mock_provider("a", {}, role_a)
        pb = _make_mock_provider("b", {}, role_b)

        hub = FeatureHub.__new__(FeatureHub)
        hub._providers = {"a": pa, "b": pb}
        hub._computed_keys = {}

        mapping = hub.role_mapping_all()
        assert mapping["feature_x"] == FeatureRole.WHY
        assert mapping["feature_y"] == FeatureRole.WHEN
        assert mapping["feature_z"] == FeatureRole.WHERE


class TestDescribe:
    """test_describe: provider 状态摘要验证。"""

    def test_describe_returns_provider_info(self) -> None:
        """describe() 返回每个 provider 的名称和 feature_count 信息。"""
        from src.research.features.hub import FeatureHub

        pa = _make_mock_provider("prov_a", {("i", "f"): [1.0]}, {})
        pa.feature_count = 3

        hub = FeatureHub.__new__(FeatureHub)
        hub._providers = {"prov_a": pa}
        hub._computed_keys = {}

        desc = hub.describe()
        assert "prov_a" in desc
        assert desc["prov_a"]["name"] == "prov_a"
        assert desc["prov_a"]["feature_count"] == 3

    def test_describe_empty_hub(self) -> None:
        """无 provider 时 describe() 返回包含 total_providers=0 的 dict。"""
        from src.research.features.hub import FeatureHub

        hub = FeatureHub.__new__(FeatureHub)
        hub._providers = {}
        hub._computed_keys = {}

        desc = hub.describe()
        assert desc["total_providers"] == 0


class TestWrongLengthRaises:
    """test_wrong_length_raises: provider 返回长度不一致的数组 → ValueError。"""

    def test_wrong_length_raises_value_error(self) -> None:
        """provider 返回长度 != matrix.n_bars 时立即抛出 ValueError。"""
        from src.research.features.hub import FeatureHub

        n = 10
        wrong_length = 5
        features: Dict[Tuple[str, str], List[Optional[float]]] = {
            ("ind", "bad"): [1.0] * wrong_length
        }
        provider = _make_mock_provider("bad_p", features, {})

        hub = FeatureHub.__new__(FeatureHub)
        hub._providers = {"bad_p": provider}
        hub._computed_keys = {}

        matrix = _make_matrix(n)
        with pytest.raises(ValueError, match="bad_p"):
            hub.compute_all(matrix)


# ---------------------------------------------------------------------------
# 集成测试：真实 config
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestInitWithRealConfig:
    """test_init_with_real_config: 使用真实 config 初始化 FeatureHub。"""

    def test_enabled_providers_match_config(self) -> None:
        """load_research_config() → FeatureHub → 验证启用的 provider 与配置一致。"""
        from src.research.features.hub import FeatureHub
        from src.research.core.config import load_research_config

        config = load_research_config()
        hub = FeatureHub(config)

        providers = hub._providers

        # 默认配置：temporal/microstructure/regime_transition/session_event/intrabar 启用
        # cross_tf 默认关闭
        fp = config.feature_providers
        expected_enabled = {
            "temporal",
            "microstructure",
            "regime_transition",
            "session_event",
            "intrabar",
        }
        expected_disabled = {"cross_tf"}

        for name in expected_enabled:
            if fp.is_enabled(name):
                assert name in providers, f"{name} 应启用但未在 providers 中"

        for name in expected_disabled:
            if not fp.is_enabled(name):
                assert name not in providers, f"{name} 应禁用但出现在 providers 中"

    def test_describe_returns_all_enabled(self) -> None:
        """describe() 覆盖所有已注册的 provider。"""
        from src.research.features.hub import FeatureHub
        from src.research.core.config import load_research_config

        config = load_research_config()
        hub = FeatureHub(config)
        desc = hub.describe()

        for name in hub._providers:
            assert name in desc

    def test_required_extra_data_is_list(self) -> None:
        """required_extra_data() 返回列表类型。"""
        from src.research.features.hub import FeatureHub
        from src.research.core.config import load_research_config

        config = load_research_config()
        hub = FeatureHub(config)
        reqs = hub.required_extra_data()
        assert isinstance(reqs, list)

    def test_role_mapping_all_returns_dict(self) -> None:
        """role_mapping_all() 返回字典类型。"""
        from src.research.features.hub import FeatureHub
        from src.research.core.config import load_research_config

        config = load_research_config()
        hub = FeatureHub(config)
        mapping = hub.role_mapping_all()
        assert isinstance(mapping, dict)
