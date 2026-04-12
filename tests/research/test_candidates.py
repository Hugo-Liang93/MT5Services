from __future__ import annotations

from datetime import datetime, timezone

from src.research.core.contracts import (
    IndicatorPredictiveResult,
    MiningResult,
    PromotionDecision,
    RobustnessTier,
    RollingICResult,
    ThresholdSweepResult,
)
from src.research.strategies import discover_strategy_candidates


def _predictive_result(
    *,
    indicator_name: str,
    field_name: str,
    ic: float,
    regime: str | None = "ranging",
) -> IndicatorPredictiveResult:
    return IndicatorPredictiveResult(
        indicator_name=indicator_name,
        field_name=field_name,
        forward_bars=10,
        regime=regime,
        n_samples=240,
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
            information_ratio=0.9,
            n_windows=6,
            effective_n_windows=4.5,
        ),
        permutation_p_value=0.02,
    )


def _threshold_result(
    *,
    indicator_name: str,
    field_name: str,
    regime: str | None = "ranging",
) -> ThresholdSweepResult:
    return ThresholdSweepResult(
        indicator_name=indicator_name,
        field_name=field_name,
        forward_bars=10,
        regime=regime,
        optimal_buy_threshold=35.0,
        buy_hit_rate=0.61,
        buy_mean_return=0.002,
        buy_n_signals=95,
        optimal_sell_threshold=None,
        sell_hit_rate=0.0,
        sell_mean_return=0.0,
        sell_n_signals=0,
        cv_consistency_buy=0.78,
        test_buy_hit_rate=0.57,
        test_buy_n_signals=40,
        is_significant_buy=True,
        permutation_p_buy=0.03,
    )


class _Rule:
    def __init__(
        self,
        *,
        indicator_name: str,
        field_name: str,
        regime: str | None = "ranging",
    ) -> None:
        self.regime = regime
        self.direction = "buy"
        self.test_hit_rate = 0.58
        self.test_n_samples = 33
        self.structured = {
            "why": [
                {
                    "indicator": "adx14",
                    "field": "adx",
                    "operator": ">=",
                    "threshold": 25.0,
                }
            ],
            "when": [
                {
                    "indicator": indicator_name,
                    "field": field_name,
                    "operator": "<=",
                    "threshold": 35.0,
                }
            ],
            "where": [
                {
                    "indicator": "boll20",
                    "field": "lower_band_pct",
                    "operator": "<=",
                    "threshold": 0.15,
                }
            ],
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
    *,
    rule_indicator_name: str,
    rule_field_name: str,
) -> MiningResult:
    return MiningResult(
        run_id=run_id,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        completed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        predictive_power=predictive_items,
        threshold_sweeps=threshold_items,
        mined_rules=[
            _Rule(
                indicator_name=rule_indicator_name,
                field_name=rule_field_name,
            )
        ],
    )


def test_discover_strategy_candidates_classifies_robust_tf_specific_and_divergent() -> None:
    results = {
        "M30": _mining_result(
            "run_m30",
            [
                _predictive_result(
                    indicator_name="rsi14",
                    field_name="rsi",
                    ic=0.18,
                ),
                _predictive_result(
                    indicator_name="stoch14",
                    field_name="k",
                    ic=0.15,
                ),
            ],
            [
                _threshold_result(
                    indicator_name="rsi14",
                    field_name="rsi",
                ),
                _threshold_result(
                    indicator_name="stoch14",
                    field_name="k",
                ),
            ],
            rule_indicator_name="rsi14",
            rule_field_name="rsi",
        ),
        "H1": _mining_result(
            "run_h1",
            [
                _predictive_result(
                    indicator_name="rsi14",
                    field_name="rsi",
                    ic=0.14,
                ),
                _predictive_result(
                    indicator_name="stoch14",
                    field_name="k",
                    ic=-0.17,
                ),
            ],
            [
                _threshold_result(
                    indicator_name="rsi14",
                    field_name="rsi",
                ),
                _threshold_result(
                    indicator_name="stoch14",
                    field_name="k",
                ),
            ],
            rule_indicator_name="rsi14",
            rule_field_name="rsi",
        ),
        "H4": _mining_result(
            "run_h4",
            [
                _predictive_result(
                    indicator_name="macd",
                    field_name="hist",
                    ic=-0.22,
                )
            ],
            [
                _threshold_result(
                    indicator_name="macd",
                    field_name="hist",
                )
            ],
            rule_indicator_name="macd",
            rule_field_name="hist",
        ),
    }

    discovery = discover_strategy_candidates(results, symbol="XAUUSD")
    robust = next(
        spec
        for spec in discovery.candidate_specs
        if spec.robustness_tier is RobustnessTier.ROBUST
    )
    tf_specific = next(
        spec
        for spec in discovery.candidate_specs
        if spec.robustness_tier is RobustnessTier.TF_SPECIFIC
    )
    divergent = next(
        spec
        for spec in discovery.candidate_specs
        if spec.robustness_tier is RobustnessTier.DIVERGENT
    )

    assert robust.allowed_timeframes == ("H1", "M30")
    assert robust.promotion_decision is PromotionDecision.REFIT
    assert robust.key_indicator == "rsi14.rsi"
    assert "predictive_power" in robust.evidence_types
    assert "threshold" in robust.evidence_types

    assert tf_specific.target_timeframe == "H4"
    assert tf_specific.allowed_timeframes == ("H4",)
    assert tf_specific.key_indicator == "macd.hist"
    assert tf_specific.direction == "sell"
    assert tf_specific.promotion_decision is PromotionDecision.REFIT

    assert divergent.key_indicator == "stoch14.k"
    assert divergent.promotion_decision is PromotionDecision.REJECT
    assert "cross-TF direction diverges" in divergent.decision_reason


def test_discover_strategy_candidates_marks_tf_specific_without_multiple_evidence_as_refit() -> None:
    results = {
        "H1": _mining_result(
            "run_h1",
            [
                _predictive_result(
                    indicator_name="rsi14",
                    field_name="rsi",
                    ic=0.16,
                )
            ],
            [],
            rule_indicator_name="adx14",
            rule_field_name="adx",
        )
    }

    discovery = discover_strategy_candidates(results, symbol="XAUUSD")

    assert len(discovery.candidate_specs) == 1
    candidate = discovery.candidate_specs[0]
    assert candidate.robustness_tier is RobustnessTier.TF_SPECIFIC
    assert candidate.promotion_decision is PromotionDecision.REFIT
    assert "insufficient evidence types" in candidate.decision_reason
