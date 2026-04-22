"""Analyzer Protocol + 注册表契约测试（P2）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.research.analyzers import (
    Analyzer,
    all_analyzer_names,
    get_analyzer,
    register_analyzer,
)
from src.research.analyzers.default_analyzers import (
    PredictivePowerAnalyzer,
    RuleMiningAnalyzer,
    ThresholdSweepAnalyzer,
    register_default_analyzers,
)
from src.research.analyzers.protocol import clear_registry


@pytest.fixture(autouse=True)
def _restore_registry():
    """每个测试后还原默认注册（测试间隔离）。"""
    yield
    clear_registry()
    register_default_analyzers()


# ── Protocol 契约 ────────────────────────────────────────────────────


class TestAnalyzerProtocol:
    def test_default_analyzers_satisfy_protocol(self) -> None:
        for cls in (
            PredictivePowerAnalyzer,
            ThresholdSweepAnalyzer,
            RuleMiningAnalyzer,
        ):
            instance = cls()
            assert isinstance(instance, Analyzer), f"{cls.__name__} 不满足 Protocol"

    def test_default_analyzer_metadata(self) -> None:
        pp = PredictivePowerAnalyzer()
        assert pp.name == "predictive_power"
        assert pp.result_field == "predictive_power"
        assert pp.requires_provider_groups is True

        ts = ThresholdSweepAnalyzer()
        assert ts.name == "threshold"
        assert ts.result_field == "threshold_sweeps"
        assert ts.requires_provider_groups is False

        rm = RuleMiningAnalyzer()
        assert rm.name == "rule_mining"
        assert rm.result_field == "mined_rules"
        assert rm.requires_provider_groups is True


# ── 注册表 ───────────────────────────────────────────────────────────


class TestRegistry:
    def test_default_analyzers_auto_registered(self) -> None:
        names = all_analyzer_names()
        assert "predictive_power" in names
        assert "threshold" in names
        assert "rule_mining" in names

    def test_get_unknown_raises_keyerror(self) -> None:
        with pytest.raises(KeyError, match="unknown_xxx"):
            get_analyzer("unknown_xxx")

    def test_register_custom_analyzer(self) -> None:
        class _CustomAnalyzer:
            name = "custom_x"
            result_field = "custom_x_results"  # MiningResult 上的字段名
            requires_provider_groups = False

            def analyze(
                self,
                matrix: Any,
                *,
                config: Any,
                provider_groups: Optional[Dict[str, List[Tuple[str, str]]]] = None,
                indicator_filter: Optional[List[str]] = None,
            ) -> List[Any]:
                return ["custom"]

        register_analyzer(_CustomAnalyzer())
        assert "custom_x" in all_analyzer_names()
        analyzer = get_analyzer("custom_x")
        assert analyzer.name == "custom_x"
        assert analyzer.analyze(None, config=None) == ["custom"]

    def test_register_rejects_non_protocol(self) -> None:
        class _NotAnAnalyzer:
            pass

        with pytest.raises(TypeError, match="Protocol"):
            register_analyzer(_NotAnAnalyzer())  # type: ignore[arg-type]

    def test_register_overwrites_same_name(self) -> None:
        class _A1:
            name = "dup"
            result_field = "dup"
            requires_provider_groups = False

            def analyze(self, matrix, *, config, **_):
                return "first"

        class _A2:
            name = "dup"
            result_field = "dup"
            requires_provider_groups = False

            def analyze(self, matrix, *, config, **_):
                return "second"

        register_analyzer(_A1())
        register_analyzer(_A2())
        analyzer = get_analyzer("dup")
        assert analyzer.analyze(None, config=None) == "second"

    def test_clear_then_register_default(self) -> None:
        clear_registry()
        assert all_analyzer_names() == []
        register_default_analyzers()
        assert set(all_analyzer_names()) == {
            "predictive_power",
            "barrier_predictive_power",
            "threshold",
            "rule_mining",
        }
