"""tests/research/features/test_protocol.py

FeatureProvider protocol 及相关类型的单元测试。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.research.features.protocol import (
    PROMOTED_INDICATOR_PRECEDENTS,
    FeatureComputeResult,
    FeatureProvider,
    FeatureRole,
    ProviderDataRequirement,
)


# ---------------------------------------------------------------------------
# FeatureRole
# ---------------------------------------------------------------------------


class TestFeatureRole:
    def test_enum_values(self) -> None:
        assert FeatureRole.WHY.value == "why"
        assert FeatureRole.WHEN.value == "when"
        assert FeatureRole.WHERE.value == "where"
        assert FeatureRole.VOLUME.value == "volume"

    def test_all_members(self) -> None:
        members = {r.value for r in FeatureRole}
        assert members == {"why", "when", "where", "volume"}


# ---------------------------------------------------------------------------
# ProviderDataRequirement
# ---------------------------------------------------------------------------


class TestProviderDataRequirement:
    def test_basic_creation(self) -> None:
        req = ProviderDataRequirement(
            parent_tf_mapping={"M30": "H4"},
            parent_indicators=["supertrend_direction", "rsi14"],
        )
        assert req.parent_tf_mapping == {"M30": "H4"}
        assert req.parent_indicators == ["supertrend_direction", "rsi14"]

    def test_empty_fields(self) -> None:
        req = ProviderDataRequirement(parent_tf_mapping={}, parent_indicators=[])
        assert req.parent_tf_mapping == {}
        assert req.parent_indicators == []

    def test_frozen(self) -> None:
        req = ProviderDataRequirement(
            parent_tf_mapping={"H1": "H4"}, parent_indicators=[]
        )
        with pytest.raises((AttributeError, TypeError)):
            req.parent_tf_mapping = {}  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FeatureComputeResult
# ---------------------------------------------------------------------------


class TestFeatureComputeResult:
    def test_initial_state(self) -> None:
        result = FeatureComputeResult()
        assert result.provider_summaries == {}
        assert result.total_features == 0

    def test_add_single_provider(self) -> None:
        result = FeatureComputeResult()
        result.add("trend_provider", feature_count=5, elapsed_sec=0.12)
        assert "trend_provider" in result.provider_summaries
        summary = result.provider_summaries["trend_provider"]
        assert summary["feature_count"] == 5
        assert summary["elapsed_sec"] == pytest.approx(0.12)

    def test_total_features_accumulates(self) -> None:
        result = FeatureComputeResult()
        result.add("p1", feature_count=3, elapsed_sec=0.1)
        result.add("p2", feature_count=7, elapsed_sec=0.2)
        assert result.total_features == 10

    def test_add_multiple_providers(self) -> None:
        result = FeatureComputeResult()
        result.add("a", feature_count=2, elapsed_sec=0.05)
        result.add("b", feature_count=4, elapsed_sec=0.08)
        result.add("c", feature_count=1, elapsed_sec=0.02)
        assert result.total_features == 7
        assert len(result.provider_summaries) == 3

    def test_to_dict(self) -> None:
        result = FeatureComputeResult()
        result.add("prov_x", feature_count=6, elapsed_sec=0.33)
        d = result.to_dict()
        assert "total_features" in d
        assert d["total_features"] == 6
        assert "providers" in d
        assert "prov_x" in d["providers"]


# ---------------------------------------------------------------------------
# FeatureProvider protocol
# ---------------------------------------------------------------------------


class TestFeatureProviderProtocol:
    def test_protocol_not_directly_instantiable(self) -> None:
        """Protocol 本身不应可直接实例化（runtime_checkable 下 isinstance 可用，
        但直接调用 FeatureProvider() 应失败）。"""
        with pytest.raises(TypeError):
            FeatureProvider()  # type: ignore[call-arg]

    def test_concrete_class_satisfies_protocol(self) -> None:
        """实现所有方法的具体类应满足 Protocol。"""
        from typing import runtime_checkable

        class ConcreteProvider:
            @property
            def name(self) -> str:
                return "concrete"

            @property
            def feature_count(self) -> int:
                return 2

            def required_columns(self) -> List[Tuple[str, str]]:
                return [("rsi14", "rsi")]

            def required_extra_data(self) -> Optional[ProviderDataRequirement]:
                return None

            def role_mapping(self) -> Dict[str, FeatureRole]:
                return {"rsi_signal": FeatureRole.WHY}

            def compute(
                self,
                matrix: Any,
                extra_data: Optional[Dict[str, Any]] = None,
            ) -> Dict[Tuple[str, str], List[Optional[float]]]:
                return {("rsi14", "rsi_signal"): [0.5, 0.6]}

        provider = ConcreteProvider()
        # runtime_checkable isinstance check
        assert isinstance(provider, FeatureProvider)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PROMOTED_INDICATOR_PRECEDENTS
# ---------------------------------------------------------------------------


class TestPromotedIndicatorPrecedents:
    def test_is_tuple(self) -> None:
        assert isinstance(PROMOTED_INDICATOR_PRECEDENTS, tuple)

    def test_has_five_entries(self) -> None:
        assert len(PROMOTED_INDICATOR_PRECEDENTS) == 5

    def test_required_keys(self) -> None:
        for entry in PROMOTED_INDICATOR_PRECEDENTS:
            assert "feature_name" in entry
            assert "promoted_indicator_name" in entry
            assert "status" in entry
            assert entry["status"] == "promoted_indicator"

    def test_known_entries(self) -> None:
        names = {e["feature_name"] for e in PROMOTED_INDICATOR_PRECEDENTS}
        assert "di_spread" in names
        assert "squeeze_score" in names
        assert "vwap_gap_atr" in names
        assert "rsi_accel" in names
        assert "momentum_consensus" in names
