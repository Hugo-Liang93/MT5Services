from __future__ import annotations

import hashlib
from typing import Any, Dict, Mapping, Sequence

from ..core.contracts import (
    CandidateEvidence,
    FeatureCandidateDiscoveryResult,
    FeatureCandidateSpec,
    IndicatorPromotionDecision,
    MiningResult,
    RobustnessTier,
)
from ..core.cross_tf import analyze_cross_tf
from .engineer import build_default_engineer


_FEATURE_VALIDATION_GATES: dict[str, Any] = {
    "research": {
        "required_evidence_types": 2,
        "require_no_lookahead": True,
        "require_bar_close_computable": True,
        "require_bounded_lookback": True,
        "require_live_computable": True,
        "require_interpretable": True,
    },
    "backtest_validation_required": True,
    "monte_carlo_required": True,
    "walk_forward_required": True,
}


def discover_feature_candidates(
    results_by_timeframe: Mapping[str, MiningResult],
    *,
    symbol: str,
    min_tfs_for_robust: int = 2,
    candidate_limit: int = 12,
) -> FeatureCandidateDiscoveryResult:
    analysis = analyze_cross_tf(
        dict(results_by_timeframe),
        min_tfs_for_robust=min_tfs_for_robust,
    )
    engineer = build_default_engineer()
    seed_map = _collect_feature_seed_map(results_by_timeframe)
    candidates: list[FeatureCandidateSpec] = []

    for (feature_name, regime), seed in seed_map.items():
        definition = engineer.definition(feature_name)
        if definition is None:
            continue

        target_tf = _recommend_seed_timeframe(seed)
        target_result = results_by_timeframe.get(target_tf)
        if target_result is None:
            continue

        tier = _resolve_seed_tier(seed)
        direction_hint = _resolve_seed_direction(seed.get(target_tf, {}))
        evidence = _collect_candidate_evidence(
            target_result=target_result,
            target_timeframe=target_tf,
            feature_name=feature_name,
            regime=regime,
            direction_hint=direction_hint,
        )
        decision = _resolve_indicator_promotion_decision(
            tier=tier,
            definition=definition,
            evidence=evidence,
        )
        allowed_timeframes = (
            tuple(sorted(seed.keys()))
            if tier is RobustnessTier.ROBUST
            else (target_tf,)
        )
        candidates.append(
            FeatureCandidateSpec(
                candidate_id=_build_candidate_id(
                    symbol=symbol,
                    timeframe=target_tf,
                    feature_name=feature_name,
                    regime=regime,
                ),
                symbol=symbol,
                target_timeframe=target_tf,
                allowed_timeframes=allowed_timeframes,
                feature_name=feature_name,
                formula_summary=definition.formula_summary,
                dependencies=tuple(definition.source_inputs),
                runtime_state_inputs=tuple(definition.runtime_state_inputs),
                live_computable=definition.live_computable,
                compute_scope=definition.compute_scope,
                robustness_tier=tier,
                direction_hint=direction_hint,
                strategy_roles=tuple(definition.strategy_roles),
                evidence=tuple(evidence),
                validation_gates=_validation_gates_for(tier),
                promotion_decision=decision,
                research_provenance=",".join(
                    sorted(result.run_id for result in results_by_timeframe.values())
                ),
            )
        )

    candidates.sort(
        key=lambda item: (
            0 if item.promotion_decision.name.startswith("PROMOTE") else 1,
            0 if item.robustness_tier is RobustnessTier.ROBUST else 1,
            item.target_timeframe,
            item.feature_name,
        )
    )
    return FeatureCandidateDiscoveryResult(
        symbol=symbol,
        timeframes=tuple(sorted(results_by_timeframe.keys())),
        feature_candidates=tuple(candidates[:candidate_limit]),
        cross_tf_analysis=analysis.to_dict(),
        registry_inventory=engineer.inventory(),
    )


def _collect_candidate_evidence(
    *,
    target_result: MiningResult,
    target_timeframe: str,
    feature_name: str,
    regime: str | None,
    direction_hint: str | None,
) -> list[CandidateEvidence]:
    evidence: list[CandidateEvidence] = []

    predictive = [
        item
        for item in target_result.predictive_power
        if item.indicator_name == "derived"
        and item.field_name == feature_name
        and item.regime == regime
        and (item.is_significant or item.p_value <= 0.05)
    ]
    predictive.sort(
        key=lambda item: (abs(item.information_coefficient), item.n_samples),
        reverse=True,
    )
    if predictive:
        best = predictive[0]
        evidence.append(
            CandidateEvidence(
                evidence_type="predictive_power",
                timeframe=target_timeframe,
                summary=(
                    f"derived.{feature_name} IC={best.information_coefficient:+.3f} "
                    f"n={best.n_samples} horizon={best.forward_bars}"
                ),
                direction=("buy" if best.information_coefficient > 0 else "sell"),
                regime=best.regime,
                detail=best.to_dict(),
            )
        )

    for sweep in target_result.threshold_sweeps:
        if sweep.indicator_name != "derived" or sweep.field_name != feature_name:
            continue
        if sweep.regime != regime:
            continue
        if direction_hint == "buy" and sweep.optimal_buy_threshold is not None:
            evidence.append(
                CandidateEvidence(
                    evidence_type="threshold_sweep",
                    timeframe=target_timeframe,
                    summary=(
                        f"BUY threshold={sweep.optimal_buy_threshold:.3f} "
                        f"hit={sweep.buy_hit_rate:.1%} n={sweep.buy_n_signals}"
                    ),
                    direction="buy",
                    regime=sweep.regime,
                    detail=sweep.to_dict(),
                )
            )
        elif direction_hint == "sell" and sweep.optimal_sell_threshold is not None:
            evidence.append(
                CandidateEvidence(
                    evidence_type="threshold_sweep",
                    timeframe=target_timeframe,
                    summary=(
                        f"SELL threshold={sweep.optimal_sell_threshold:.3f} "
                        f"hit={sweep.sell_hit_rate:.1%} n={sweep.sell_n_signals}"
                    ),
                    direction="sell",
                    regime=sweep.regime,
                    detail=sweep.to_dict(),
                )
            )

    for rule in target_result.mined_rules:
        if getattr(rule, "regime", None) != regime:
            continue
        structured = getattr(rule, "structured", {}) or {}
        if not _rule_mentions_feature(structured, feature_name):
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
                regime=regime,
                detail=rule.to_dict(),
            )
        )
        break

    return evidence


def _collect_feature_seed_map(
    results_by_timeframe: Mapping[str, MiningResult],
) -> dict[tuple[str, str | None], dict[str, dict[str, Any]]]:
    seed_map: dict[tuple[str, str | None], dict[str, dict[str, Any]]] = {}
    for timeframe, result in results_by_timeframe.items():
        for item in result.predictive_power:
            if item.indicator_name != "derived":
                continue
            if not (item.is_significant or item.p_value <= 0.05):
                continue
            direction = "buy" if item.information_coefficient > 0 else "sell"
            _register_seed(
                seed_map,
                feature_name=item.field_name,
                regime=item.regime,
                timeframe=timeframe,
                direction=direction,
                score=abs(float(item.information_coefficient)),
                source="predictive_power",
            )
        for sweep in result.threshold_sweeps:
            if sweep.indicator_name != "derived":
                continue
            if sweep.is_significant_buy and sweep.optimal_buy_threshold is not None:
                _register_seed(
                    seed_map,
                    feature_name=sweep.field_name,
                    regime=sweep.regime,
                    timeframe=timeframe,
                    direction="buy",
                    score=max(abs(float(sweep.buy_mean_return)), float(sweep.buy_hit_rate)),
                    source="threshold_sweep",
                )
            if sweep.is_significant_sell and sweep.optimal_sell_threshold is not None:
                _register_seed(
                    seed_map,
                    feature_name=sweep.field_name,
                    regime=sweep.regime,
                    timeframe=timeframe,
                    direction="sell",
                    score=max(abs(float(sweep.sell_mean_return)), float(sweep.sell_hit_rate)),
                    source="threshold_sweep",
                )
        for rule in result.mined_rules:
            structured = getattr(rule, "structured", {}) or {}
            direction = str(getattr(rule, "direction", "")).strip().lower()
            if direction not in {"buy", "sell"}:
                continue
            for feature_name in _features_mentioned_by_rule(structured):
                _register_seed(
                    seed_map,
                    feature_name=feature_name,
                    regime=getattr(rule, "regime", None),
                    timeframe=timeframe,
                    direction=direction,
                    score=float(getattr(rule, "test_hit_rate", 0.0) or 0.0),
                    source="rule_mining",
                )
    return seed_map


def _register_seed(
    seed_map: dict[tuple[str, str | None], dict[str, dict[str, Any]]],
    *,
    feature_name: str,
    regime: str | None,
    timeframe: str,
    direction: str,
    score: float,
    source: str,
) -> None:
    key = (feature_name, regime)
    timeframe_bucket = seed_map.setdefault(key, {}).setdefault(
        timeframe,
        {"votes": [], "sources": set(), "total_score": 0.0},
    )
    vote = 1.0 if direction == "buy" else -1.0
    timeframe_bucket["votes"].append(vote * max(score, 1e-6))
    timeframe_bucket["sources"].add(source)
    timeframe_bucket["total_score"] += max(score, 1e-6)


def _features_mentioned_by_rule(structured: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    for role in ("why", "when", "where"):
        for item in structured.get(role, []) or []:
            if str(item.get("indicator", "")).strip() != "derived":
                continue
            field = str(item.get("field", "")).strip()
            if field:
                names.add(field)
    return names


def _resolve_seed_tier(seed: Mapping[str, Mapping[str, Any]]) -> RobustnessTier:
    directions = [
        direction
        for direction in (
            _resolve_seed_direction(entry)
            for entry in seed.values()
        )
        if direction is not None
    ]
    if len(directions) >= 2 and len(set(directions)) == 1:
        return RobustnessTier.ROBUST
    if len(directions) >= 2:
        return RobustnessTier.DIVERGENT
    return RobustnessTier.TF_SPECIFIC


def _recommend_seed_timeframe(seed: Mapping[str, Mapping[str, Any]]) -> str:
    ranked = sorted(
        seed.items(),
        key=lambda item: (
            float(item[1].get("total_score", 0.0)),
            len(item[1].get("sources", ())),
            item[0],
        ),
        reverse=True,
    )
    return ranked[0][0]


def _resolve_indicator_promotion_decision(
    *,
    tier: RobustnessTier,
    definition: Any,
    evidence: Sequence[CandidateEvidence],
) -> IndicatorPromotionDecision:
    evidence_types = {item.evidence_type for item in evidence if item.evidence_type}
    if tier is RobustnessTier.DIVERGENT:
        return IndicatorPromotionDecision.REJECT
    if not getattr(definition, "no_lookahead", True):
        return IndicatorPromotionDecision.REJECT
    if str(getattr(definition, "compute_scope", "")).lower() != "bar_close":
        return IndicatorPromotionDecision.RESEARCH_ONLY
    if not getattr(definition, "bounded_lookback", False):
        return IndicatorPromotionDecision.REJECT
    if not getattr(definition, "interpretable", True):
        return IndicatorPromotionDecision.REJECT
    if not getattr(definition, "live_computable", False):
        return IndicatorPromotionDecision.RESEARCH_ONLY
    if len(evidence_types) < 2:
        return IndicatorPromotionDecision.REFIT

    target = str(getattr(definition, "promotion_target_default", "")).strip().lower()
    if target == "strategy_helper":
        return IndicatorPromotionDecision.RESEARCH_ONLY
    if target == "indicator_and_strategy_candidate":
        return IndicatorPromotionDecision.PROMOTE_INDICATOR_AND_STRATEGY_CANDIDATE
    if target == "shared_indicator":
        return IndicatorPromotionDecision.PROMOTE_INDICATOR
    return IndicatorPromotionDecision.RESEARCH_ONLY


def _resolve_signal_direction(signal: Any) -> str | None:
    if not getattr(signal, "tf_scores", None):
        return None
    avg_ic = sum(score.ic for score in signal.tf_scores) / max(1, len(signal.tf_scores))
    if avg_ic > 0:
        return "buy"
    if avg_ic < 0:
        return "sell"
    return None


def _resolve_seed_direction(entry: Mapping[str, Any]) -> str | None:
    votes = entry.get("votes", []) or []
    net = sum(float(vote) for vote in votes)
    if net > 0:
        return "buy"
    if net < 0:
        return "sell"
    return None


def _rule_mentions_feature(structured: Mapping[str, Any], feature_name: str) -> bool:
    for role in ("why", "when", "where"):
        for item in structured.get(role, []) or []:
            if str(item.get("indicator", "")).strip() != "derived":
                continue
            if str(item.get("field", "")).strip() == feature_name:
                return True
    return False


def _validation_gates_for(tier: RobustnessTier) -> dict[str, Any]:
    gates = dict(_FEATURE_VALIDATION_GATES)
    gates["robustness_tier"] = tier.value
    gates["cross_tf_required"] = tier is RobustnessTier.ROBUST
    return gates


def _split_indicator(indicator: str) -> tuple[str, str]:
    if "." not in indicator:
        return indicator, ""
    parts = indicator.split(".", 1)
    return parts[0], parts[1]


def _build_candidate_id(
    *,
    symbol: str,
    timeframe: str,
    feature_name: str,
    regime: str | None,
) -> str:
    raw = f"{symbol}:{timeframe}:feature:{feature_name}:{regime or 'all'}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"feat_{digest}"
