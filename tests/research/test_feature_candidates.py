from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.research.core.contracts import (
    IndicatorPredictiveResult,
    MiningResult,
    RobustnessTier,
    RollingICResult,
    ThresholdSweepResult,
)
from src.research.features import discover_feature_candidates
from src.research.features.candidates import _FeatureMeta, _FEATURE_META_REGISTRY


def _predictive_result(
    *,
    feature_name: str,
    ic: float,
    regime: str | None = "breakout",
) -> IndicatorPredictiveResult:
    return IndicatorPredictiveResult(
        indicator_name="derived",
        field_name=feature_name,
        forward_bars=10,
        regime=regime,
        n_samples=260,
        pearson_r=ic,
        spearman_rho=ic,
        p_value=0.01,
        hit_rate_above_median=0.58,
        hit_rate_below_median=0.42,
        information_coefficient=ic,
        is_significant=True,
        rolling_ic=RollingICResult(
            mean_ic=ic,
            std_ic=0.05,
            information_ratio=0.85,
            n_windows=6,
            effective_n_windows=4.2,
        ),
        permutation_p_value=0.03,
    )


def _threshold_result(
    *,
    feature_name: str,
    regime: str | None = "breakout",
) -> ThresholdSweepResult:
    return ThresholdSweepResult(
        indicator_name="derived",
        field_name=feature_name,
        forward_bars=10,
        regime=regime,
        optimal_buy_threshold=0.34,
        buy_hit_rate=0.60,
        buy_mean_return=0.002,
        buy_n_signals=92,
        optimal_sell_threshold=-0.34,
        sell_hit_rate=0.59,
        sell_mean_return=-0.002,
        sell_n_signals=88,
        cv_consistency_buy=0.76,
        cv_consistency_sell=0.72,
        test_buy_hit_rate=0.57,
        test_sell_hit_rate=0.56,
        test_buy_n_signals=41,
        test_sell_n_signals=39,
        is_significant_buy=True,
        is_significant_sell=True,
        permutation_p_buy=0.03,
        permutation_p_sell=0.04,
    )


class _Rule:
    def __init__(
        self,
        *,
        feature_name: str,
        regime: str | None = "breakout",
    ) -> None:
        self.regime = regime
        self.direction = "buy"
        self.test_hit_rate = 0.59
        self.test_n_samples = 40
        self.structured = {
            "why": [
                {
                    "indicator": "derived",
                    "field": feature_name,
                    "operator": ">=",
                    "threshold": 0.34,
                }
            ],
            "when": [],
            "where": [],
        }

    def to_dict(self) -> dict:
        return {
            "regime": self.regime,
            "direction": self.direction,
            "test_hit_rate": self.test_hit_rate,
            "test_n_samples": self.test_n_samples,
            "structured": self.structured,
        }


def _mining_result(
    run_id: str,
    predictive_items: list,
    threshold_items: list,
    rule_feature_name: str | None,
) -> MiningResult:
    return MiningResult(
        run_id=run_id,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        completed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        predictive_power=predictive_items,
        threshold_sweeps=threshold_items,
        mined_rules=[_Rule(feature_name=rule_feature_name)] if rule_feature_name else [],
    )


def test_discover_feature_candidates_emits_promotable_and_guarded_candidates() -> None:
    results = {
        "M30": _mining_result(
            "run_m30",
            [
                _predictive_result(feature_name="momentum_consensus", ic=0.18),
                _predictive_result(feature_name="bars_in_regime", ic=0.14),
            ],
            [
                _threshold_result(feature_name="momentum_consensus"),
                _threshold_result(feature_name="bars_in_regime"),
            ],
            "momentum_consensus",
        ),
        "H1": _mining_result(
            "run_h1",
            [
                _predictive_result(feature_name="momentum_consensus", ic=0.15),
                _predictive_result(feature_name="bars_in_regime", ic=-0.17),
                _predictive_result(feature_name="regime_entropy", ic=0.12),
            ],
            [
                _threshold_result(feature_name="momentum_consensus"),
                _threshold_result(feature_name="bars_in_regime"),
                _threshold_result(feature_name="regime_entropy"),
            ],
            "momentum_consensus",
        ),
    }

    discovery = discover_feature_candidates(results, symbol="XAUUSD")
    robust = next(
        item
        for item in discovery.feature_candidates
        if item.feature_name == "momentum_consensus"
    )
    divergent = next(
        item
        for item in discovery.feature_candidates
        if item.feature_name == "bars_in_regime"
    )
    tf_specific = next(
        item
        for item in discovery.feature_candidates
        if item.feature_name == "regime_entropy"
    )

    assert robust.robustness_tier is RobustnessTier.ROBUST
    assert robust.allowed_timeframes == ("H1", "M30")
    assert robust.promotion_decision.value == "promote_indicator_and_strategy_candidate"
    assert robust.live_computable is True

    assert divergent.robustness_tier is RobustnessTier.DIVERGENT
    assert divergent.promotion_decision.value == "reject"

    assert tf_specific.robustness_tier is RobustnessTier.TF_SPECIFIC
    assert tf_specific.promotion_decision.value == "research_only"
    assert tf_specific.live_computable is False


def test_discover_feature_candidates_marks_insufficient_evidence_as_refit() -> None:
    results = {
        "H1": _mining_result(
            "run_h1",
            [_predictive_result(feature_name="momentum_consensus", ic=0.16)],
            [],
            None,
        )
    }

    discovery = discover_feature_candidates(results, symbol="XAUUSD")
    assert len(discovery.feature_candidates) == 1
    assert discovery.feature_candidates[0].promotion_decision.value == "refit"


def test_discover_feature_candidates_rejects_non_interpretable_or_unbounded_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opaque_meta = _FeatureMeta(
        formula_summary="opaque",
        source_inputs=(),
        runtime_state_inputs=(),
        live_computable=True,
        compute_scope="bar_close",
        bounded_lookback=False,
        strategy_roles=("why",),
        promotion_target_default="shared_indicator",
        no_lookahead=False,
        interpretable=False,
    )
    patched_registry = {**_FEATURE_META_REGISTRY, "opaque_alpha": opaque_meta}
    monkeypatch.setattr(
        "src.research.features.candidates._FEATURE_META_REGISTRY",
        patched_registry,
    )
    results = {
        "H1": _mining_result(
            "run_h1",
            [_predictive_result(feature_name="opaque_alpha", ic=0.14)],
            [_threshold_result(feature_name="opaque_alpha")],
            "opaque_alpha",
        )
    }

    discovery = discover_feature_candidates(results, symbol="XAUUSD")
    assert len(discovery.feature_candidates) == 1
    assert discovery.feature_candidates[0].promotion_decision.value == "reject"
