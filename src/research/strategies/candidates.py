from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, Mapping, Sequence

from ..analyzers.multi_tf_aggregator import CrossTFAnalysis, analyze_cross_tf
from ..core.contracts import (
    CandidateDiscoveryResult,
    CandidateEvidence,
    CandidateRuleCondition,
    MiningResult,
    PromotionDecision,
    RobustnessTier,
    StrategyCandidateSpec,
)

_TF_SPECIFIC_VALIDATION_GATES: dict[str, Any] = {
    "min_history_months": 6,
    "min_walk_forward_splits": 5,
    "required_evidence_types": 2,
    "backtest": {
        "profit_factor_min": 1.20,
        "expectancy_min": 0.0,
        "max_drawdown_max": 0.15,
        "total_trades_min": 80,
    },
    "execution_feasibility": {
        "accepted_entry_ratio_min": 0.85,
        "reject_reasons_blocklist": [
            "below_min_volume_for_execution_feasibility",
            "margin_guard_block",
        ],
    },
    "monte_carlo": {
        "max_p_value": 0.10,
        "allowed_metrics": ["sharpe_ratio", "profit_factor"],
    },
    "walk_forward": {
        "overfitting_ratio_max": 1.50,
        "consistency_rate_min": 0.70,
        "oos_trades_min": 60,
        "oos_profit_factor_min": 1.10,
        "oos_max_drawdown_max": 0.15,
    },
    "demo_validation": {
        "min_trades": 20,
        "required_verdict": "pass",
    },
}

_ROBUST_VALIDATION_GATES: dict[str, Any] = {
    "min_history_months": 6,
    "min_walk_forward_splits": 5,
    "required_evidence_types": 1,
    "backtest": {
        "profit_factor_min": 1.10,
        "expectancy_min": 0.0,
        "max_drawdown_max": 0.18,
        "total_trades_min": 60,
    },
    "execution_feasibility": {
        "accepted_entry_ratio_min": 0.80,
    },
    "monte_carlo": {
        "max_p_value": 0.10,
        "allowed_metrics": ["sharpe_ratio", "profit_factor"],
    },
    "walk_forward": {
        "overfitting_ratio_max": 1.60,
        "consistency_rate_min": 0.60,
        "oos_trades_min": 50,
        "oos_profit_factor_min": 1.05,
        "oos_max_drawdown_max": 0.18,
    },
    "demo_validation": {
        "min_trades": 20,
        "required_verdict": "pass",
    },
}


def discover_strategy_candidates(
    results_by_timeframe: Mapping[str, MiningResult],
    *,
    symbol: str,
    min_tfs_for_robust: int = 2,
    candidate_limit: int = 12,
) -> CandidateDiscoveryResult:
    analysis = analyze_cross_tf(
        dict(results_by_timeframe), min_tfs_for_robust=min_tfs_for_robust
    )
    candidates: list[StrategyCandidateSpec] = []

    for signal in (
        list(analysis.robust_signals)
        + list(analysis.tf_specific_signals)
        + list(analysis.divergent_signals)
    ):
        tier = signal.robustness
        target_tf = signal.recommended_tf
        target_result = results_by_timeframe.get(target_tf)
        if target_result is None:
            continue

        indicator_name, field_name = _split_indicator(signal.indicator)
        direction = _resolve_signal_direction(signal)
        evidence = _collect_candidate_evidence(
            target_result=target_result,
            target_timeframe=target_tf,
            indicator_name=indicator_name,
            field_name=field_name,
            regime=signal.regime,
            direction=direction,
        )
        evidence_types = tuple(
            sorted({item.evidence_type for item in evidence if item.evidence_type})
        )
        why_conditions, when_conditions, where_conditions = _collect_rule_conditions(
            target_result=target_result,
            indicator_name=indicator_name,
            regime=signal.regime,
        )
        thresholds = _collect_threshold_hints(
            target_result=target_result,
            indicator_name=indicator_name,
            field_name=field_name,
            regime=signal.regime,
        )

        decision, reason = _resolve_promotion_decision(
            tier=tier,
            evidence_types=evidence_types,
        )

        allowed_timeframes = (
            tuple(sorted({score.timeframe for score in signal.tf_scores}))
            if tier is RobustnessTier.ROBUST
            else (target_tf,)
        )
        candidate_id = _build_candidate_id(
            symbol=symbol,
            timeframe=target_tf,
            indicator=signal.indicator,
            regime=signal.regime,
        )
        candidates.append(
            StrategyCandidateSpec(
                candidate_id=candidate_id,
                symbol=symbol,
                target_timeframe=target_tf,
                allowed_timeframes=allowed_timeframes,
                source_run_ids=tuple(
                    sorted(
                        {
                            result.run_id
                            for tf, result in results_by_timeframe.items()
                            if any(score.timeframe == tf for score in signal.tf_scores)
                        }
                    )
                ),
                robustness_tier=tier,
                promotion_decision=decision,
                key_indicator=signal.indicator,
                regime=signal.regime,
                direction=direction,
                why_conditions=tuple(why_conditions),
                when_conditions=tuple(when_conditions),
                where_conditions=tuple(where_conditions),
                thresholds=thresholds,
                dominant_sessions=tuple(),
                evidence_types=evidence_types,
                evidence=tuple(evidence),
                validation_gates=_validation_gates_for(tier),
                decision_reason=reason,
                research_provenance=",".join(
                    sorted({result.run_id for result in results_by_timeframe.values()})
                ),
            )
        )

    candidates.sort(
        key=lambda item: (
            0 if item.robustness_tier is RobustnessTier.ROBUST else 1,
            0 if item.promotion_decision is not PromotionDecision.REJECT else 1,
            item.target_timeframe,
            item.key_indicator,
        )
    )

    return CandidateDiscoveryResult(
        symbol=symbol,
        timeframes=tuple(sorted(results_by_timeframe.keys())),
        candidate_specs=tuple(candidates[:candidate_limit]),
        cross_tf_analysis=analysis.to_dict(),
    )


def _collect_candidate_evidence(
    *,
    target_result: MiningResult,
    target_timeframe: str,
    indicator_name: str,
    field_name: str,
    regime: str | None,
    direction: str | None,
) -> list[CandidateEvidence]:
    evidence: list[CandidateEvidence] = []

    predictive = _matching_predictive_results(
        target_result,
        indicator_name=indicator_name,
        field_name=field_name,
        regime=regime,
    )
    if predictive:
        best = predictive[0]
        evidence.append(
            CandidateEvidence(
                evidence_type="predictive_power",
                timeframe=target_timeframe,
                summary=(
                    f"{indicator_name}.{field_name} IC={best.information_coefficient:+.3f} "
                    f"n={best.n_samples} horizon={best.forward_bars}"
                ),
                direction=("buy" if best.information_coefficient > 0 else "sell"),
                regime=best.regime,
                detail=best.to_dict(),
            )
        )

    for sweep in target_result.threshold_sweeps:
        if sweep.indicator_name != indicator_name or sweep.field_name != field_name:
            continue
        if sweep.regime != regime:
            continue
        if direction == "buy" and sweep.optimal_buy_threshold is not None:
            evidence.append(
                CandidateEvidence(
                    evidence_type="threshold",
                    timeframe=target_timeframe,
                    summary=(
                        f"BUY threshold={sweep.optimal_buy_threshold:.2f} "
                        f"hit={sweep.buy_hit_rate:.1%} n={sweep.buy_n_signals}"
                    ),
                    direction="buy",
                    regime=sweep.regime,
                    detail=sweep.to_dict(),
                )
            )
        elif direction == "sell" and sweep.optimal_sell_threshold is not None:
            evidence.append(
                CandidateEvidence(
                    evidence_type="threshold",
                    timeframe=target_timeframe,
                    summary=(
                        f"SELL threshold={sweep.optimal_sell_threshold:.2f} "
                        f"hit={sweep.sell_hit_rate:.1%} n={sweep.sell_n_signals}"
                    ),
                    direction="sell",
                    regime=sweep.regime,
                    detail=sweep.to_dict(),
                )
            )

    for rule in target_result.mined_rules:
        rule_regime = getattr(rule, "regime", None)
        if rule_regime != regime:
            continue
        structured = getattr(rule, "structured", {}) or {}
        if not _rule_mentions_indicator(structured, indicator_name, field_name):
            continue
        evidence.append(
            CandidateEvidence(
                evidence_type="rule_mining",
                timeframe=target_timeframe,
                summary=(
                    f"{getattr(rule, 'direction', '')} "
                    f"test_hit={getattr(rule, 'test_hit_rate', 0.0):.1%} "
                    f"n={getattr(rule, 'test_n_samples', 0)}"
                ),
                direction=getattr(rule, "direction", None),
                regime=rule_regime,
                detail=rule.to_dict(),
            )
        )

    return evidence


def _collect_rule_conditions(
    *,
    target_result: MiningResult,
    indicator_name: str,
    regime: str | None,
) -> tuple[
    list[CandidateRuleCondition],
    list[CandidateRuleCondition],
    list[CandidateRuleCondition],
]:
    why: list[CandidateRuleCondition] = []
    when: list[CandidateRuleCondition] = []
    where: list[CandidateRuleCondition] = []
    for rule in target_result.mined_rules:
        if getattr(rule, "regime", None) != regime:
            continue
        structured = getattr(rule, "structured", {}) or {}
        if not _rule_mentions_indicator(structured, indicator_name, None):
            continue
        why.extend(_conditions_from_rule(structured.get("why", []), "why"))
        when.extend(_conditions_from_rule(structured.get("when", []), "when"))
        where.extend(_conditions_from_rule(structured.get("where", []), "where"))
        break
    return why[:6], when[:6], where[:6]


def _conditions_from_rule(
    items: Iterable[Mapping[str, Any]],
    role: str,
) -> list[CandidateRuleCondition]:
    conditions: list[CandidateRuleCondition] = []
    for item in items:
        try:
            conditions.append(
                CandidateRuleCondition(
                    role=role,
                    indicator=str(item.get("indicator", "")).strip(),
                    field=str(item.get("field", "")).strip(),
                    operator=str(item.get("operator", "")).strip(),
                    threshold=float(item.get("threshold", 0.0)),
                )
            )
        except (TypeError, ValueError):
            continue
    return conditions


def _collect_threshold_hints(
    *,
    target_result: MiningResult,
    indicator_name: str,
    field_name: str,
    regime: str | None,
) -> dict[str, float]:
    for sweep in target_result.threshold_sweeps:
        if sweep.indicator_name != indicator_name or sweep.field_name != field_name:
            continue
        if sweep.regime != regime:
            continue
        thresholds: dict[str, float] = {}
        if sweep.optimal_buy_threshold is not None:
            thresholds["buy"] = float(sweep.optimal_buy_threshold)
        if sweep.optimal_sell_threshold is not None:
            thresholds["sell"] = float(sweep.optimal_sell_threshold)
        return thresholds
    return {}


def _resolve_promotion_decision(
    *,
    tier: RobustnessTier,
    evidence_types: Sequence[str],
) -> tuple[PromotionDecision, str]:
    if tier is RobustnessTier.DIVERGENT:
        return (
            PromotionDecision.REJECT,
            "cross-TF direction diverges; split into independent hypotheses before promotion",
        )
    required = 2 if tier is RobustnessTier.TF_SPECIFIC else 1
    if len(set(evidence_types)) < required:
        return (
            PromotionDecision.REFIT,
            f"insufficient evidence types for {tier.value}: "
            f"{sorted(set(evidence_types))}",
        )
    return (
        PromotionDecision.REFIT,
        "research evidence passed initial gate; requires backtest, execution-feasibility "
        "and walk-forward validation before deployment",
    )


def _resolve_signal_direction(signal: Any) -> str | None:
    if not getattr(signal, "tf_scores", None):
        return None
    avg_ic = sum(score.ic for score in signal.tf_scores) / max(1, len(signal.tf_scores))
    if avg_ic > 0:
        return "buy"
    if avg_ic < 0:
        return "sell"
    return None


def _split_indicator(indicator: str) -> tuple[str, str]:
    if "." not in indicator:
        return indicator, ""
    parts = indicator.split(".", 1)
    return parts[0], parts[1]


def _matching_predictive_results(
    result: MiningResult,
    *,
    indicator_name: str,
    field_name: str,
    regime: str | None,
) -> list[Any]:
    matches = [
        item
        for item in result.predictive_power
        if item.indicator_name == indicator_name
        and item.field_name == field_name
        and item.regime == regime
        and item.is_significant
    ]
    matches.sort(
        key=lambda item: (abs(item.information_coefficient), item.n_samples),
        reverse=True,
    )
    return matches


def _rule_mentions_indicator(
    structured: Mapping[str, Any],
    indicator_name: str,
    field_name: str | None,
) -> bool:
    for role in ("why", "when", "where"):
        for item in structured.get(role, []) or []:
            if str(item.get("indicator", "")).strip() != indicator_name:
                continue
            if (
                field_name is not None
                and str(item.get("field", "")).strip() != field_name
            ):
                continue
            return True
    return False


def _validation_gates_for(tier: RobustnessTier) -> dict[str, Any]:
    if tier is RobustnessTier.TF_SPECIFIC:
        return dict(_TF_SPECIFIC_VALIDATION_GATES)
    if tier is RobustnessTier.ROBUST:
        return dict(_ROBUST_VALIDATION_GATES)
    return {
        "promotion_blocked": True,
        "reason": "divergent cross-TF signal",
    }


def _build_candidate_id(
    *,
    symbol: str,
    timeframe: str,
    indicator: str,
    regime: str | None,
) -> str:
    raw = f"{symbol}:{timeframe}:{indicator}:{regime or 'all'}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"cand_{digest}"
