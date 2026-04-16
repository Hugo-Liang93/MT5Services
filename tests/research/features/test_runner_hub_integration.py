"""tests/research/features/test_runner_hub_integration.py

MiningRunner ↔ FeatureHub 集成测试。

验证：
- MiningRunner 创建 FeatureHub（不再使用 FeatureEngineer）
- provider_groups 在运行后有值
- MiningResult 包含新字段
"""
from __future__ import annotations

import ast
import importlib
import textwrap
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from src.research.core.config import (
    FeatureProviderConfig,
    ResearchConfig,
)
from src.research.core.contracts import Finding, MiningResult


# ---------------------------------------------------------------------------
# 测试：MiningRunner 不再导入 FeatureEngineer
# ---------------------------------------------------------------------------


def test_runner_does_not_import_feature_engineer():
    """runner.py 中不应存在对 FeatureEngineer 的静态或条件导入。"""
    import src.research.orchestration.runner as runner_mod

    source = importlib.util.find_spec(runner_mod.__name__)
    assert source is not None and source.origin is not None
    with open(source.origin, "r", encoding="utf-8") as f:
        content = f.read()

    assert "from src.research.features.engineer import" not in content
    assert "build_default_engineer" not in content


# ---------------------------------------------------------------------------
# 测试：MiningRunner 创建 FeatureHub
# ---------------------------------------------------------------------------


def test_runner_creates_feature_hub():
    """MiningRunner.__init__ 应创建 self._feature_hub 实例。"""
    from src.research.orchestration.runner import MiningRunner

    config = ResearchConfig()
    runner = MiningRunner(config=config)
    assert hasattr(runner, "_feature_hub")

    from src.research.features.hub import FeatureHub

    assert isinstance(runner._feature_hub, FeatureHub)


# ---------------------------------------------------------------------------
# 测试：provider_groups 在 compute 后有值
# ---------------------------------------------------------------------------


def test_provider_groups_populated_after_compute():
    """FeatureHub.feature_names_by_provider() 在 compute_all 后应有数据。"""
    from src.research.features.hub import FeatureHub

    config = ResearchConfig(
        feature_providers=FeatureProviderConfig(
            temporal_enabled=True,
            microstructure_enabled=False,
            cross_tf_enabled=False,
            regime_transition_enabled=False,
            session_event_enabled=False,
            intrabar_enabled=False,
        )
    )
    hub = FeatureHub(config)

    # compute_all 前应为空
    assert hub.feature_names_by_provider() == {}

    # Mock DataMatrix
    matrix = MagicMock()
    matrix.n_bars = 100
    matrix.indicator_series = {}

    # 模拟 bar_times 和其他必需属性（temporal provider 需要）
    import datetime as _dt

    base = _dt.datetime(2026, 1, 1)
    matrix.bar_times = [base + _dt.timedelta(hours=i) for i in range(100)]
    # 提供 RSI 和 ADX 数据
    matrix.indicator_series[("rsi14", "rsi")] = [50.0] * 100
    matrix.indicator_series[("adx14", "adx")] = [25.0] * 100

    result = hub.compute_all(matrix)
    groups = hub.feature_names_by_provider()

    assert "temporal" in groups
    assert len(groups["temporal"]) > 0
    assert result.total_features > 0


# ---------------------------------------------------------------------------
# 测试：MiningResult 新字段
# ---------------------------------------------------------------------------


def test_mining_result_has_new_fields():
    """MiningResult 应包含 feature_compute_summary / findings_by_provider / cross_provider_rules。"""
    import datetime as _dt

    result = MiningResult(
        run_id="test_001",
        started_at=_dt.datetime.utcnow(),
    )

    # 默认值
    assert result.feature_compute_summary is None
    assert result.findings_by_provider == {}
    assert result.cross_provider_rules == []

    # 赋值
    result.feature_compute_summary = {"total_features": 10, "providers": {}}
    result.findings_by_provider = {
        "temporal": [
            Finding(
                rank=1,
                category="predictive_power",
                summary="test",
                confidence_level="medium",
                significance_score=0.5,
                action="test action",
            )
        ]
    }
    result.cross_provider_rules = [MagicMock(to_dict=lambda: {"rule": "test"})]

    d = result.to_dict()
    assert "feature_compute_summary" in d
    assert "findings_by_provider" in d
    assert "temporal" in d["findings_by_provider"]
    assert "cross_provider_rules" in d


# ---------------------------------------------------------------------------
# 测试：tag_cross_provider_rules
# ---------------------------------------------------------------------------


def test_tag_cross_provider_rules_basic():
    """tag_cross_provider_rules 应识别涉及 2+ Provider 的规则。"""
    from src.research.analyzers.rule_mining import (
        MinedRule,
        RuleCondition,
        tag_cross_provider_rules,
    )

    provider_groups = {
        "temporal": [("temporal_rsi14", "rsi_slope")],
        "microstructure": [("micro", "spread_ratio")],
    }

    # 单 Provider 规则
    single_rule = MagicMock()
    single_rule.conditions = [
        RuleCondition(
            indicator="temporal_rsi14",
            field="rsi_slope",
            operator=">",
            threshold=0.1,
        )
    ]

    # 跨 Provider 规则
    cross_rule = MagicMock()
    cross_rule.conditions = [
        RuleCondition(
            indicator="temporal_rsi14",
            field="rsi_slope",
            operator=">",
            threshold=0.1,
        ),
        RuleCondition(
            indicator="micro",
            field="spread_ratio",
            operator="<=",
            threshold=2.0,
        ),
    ]

    result = tag_cross_provider_rules(
        [single_rule, cross_rule], provider_groups
    )
    assert len(result) == 1
    assert result[0] is cross_rule


def test_tag_cross_provider_rules_empty():
    """provider_groups 为空时返回空列表。"""
    from src.research.analyzers.rule_mining import tag_cross_provider_rules

    assert tag_cross_provider_rules([MagicMock()], None) == []
    assert tag_cross_provider_rules([MagicMock()], {}) == []


# ---------------------------------------------------------------------------
# 测试：grouped FDR
# ---------------------------------------------------------------------------


def test_grouped_fdr_does_not_break_existing():
    """fdr_grouping='global' 时行为与原有逻辑一致。"""
    from src.research.analyzers.predictive_power import _apply_batch_correction
    from src.research.core.config import OverfittingConfig
    from src.research.core.contracts import IndicatorPredictiveResult

    results = [
        IndicatorPredictiveResult(
            indicator_name="rsi14",
            field_name="rsi",
            forward_bars=5,
            regime=None,
            n_samples=200,
            pearson_r=0.15,
            spearman_rho=0.12,
            p_value=0.01,
            hit_rate_above_median=0.58,
            hit_rate_below_median=0.42,
            information_coefficient=0.12,
            is_significant=False,
        )
    ]

    of_cfg = OverfittingConfig()
    corrected_global = _apply_batch_correction(
        results, of_cfg, 0.05, 1, fdr_grouping="global"
    )
    corrected_default = _apply_batch_correction(
        results, of_cfg, 0.05, 1
    )

    assert len(corrected_global) == len(corrected_default) == 1
    assert corrected_global[0].is_significant == corrected_default[0].is_significant
