from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from ..core.contracts import (
    CandidateEvidence,
    FeatureCandidateDiscoveryResult,
    FeatureCandidateSpec,
    FeatureKind,
    IndicatorPromotionDecision,
    MiningResult,
    RobustnessTier,
)
from ..core.cross_tf import analyze_cross_tf
from .protocol import PROMOTED_INDICATOR_PRECEDENTS

# ---------------------------------------------------------------------------
# 特征元数据（轻量描述，不含计算函数）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FeatureMeta:
    """candidates.py 所需的特征描述性元数据（无计算函数）。"""

    formula_summary: str = ""
    source_inputs: Tuple[str, ...] = ()
    runtime_state_inputs: Tuple[str, ...] = ()
    live_computable: bool = True
    compute_scope: str = "bar_close"
    bounded_lookback: bool = True
    strategy_roles: Tuple[str, ...] = ()
    promotion_target_default: str = "research_only"
    no_lookahead: bool = True
    interpretable: bool = True


# 特征名 → 元数据注册表（迁移自 engineer._BUILTIN_FEATURES）
_FEATURE_META_REGISTRY: Dict[str, _FeatureMeta] = {
    "momentum_consensus": _FeatureMeta(
        formula_summary="(sign(macd.hist) + sign(rsi14.rsi-50) + sign(stoch_rsi14.stoch_rsi_k-50)) / 3",
        source_inputs=("macd.hist", "rsi14.rsi", "stoch_rsi14.stoch_rsi_k"),
        live_computable=True,
        compute_scope="bar_close",
        bounded_lookback=True,
        strategy_roles=("why", "when"),
        promotion_target_default="indicator_and_strategy_candidate",
    ),
    "regime_entropy": _FeatureMeta(
        formula_summary="-Σ(p·ln(p)) from soft regime probabilities",
        runtime_state_inputs=("soft_regimes",),
        live_computable=False,
        compute_scope="runtime_state",
        strategy_roles=("why",),
        promotion_target_default="research_only",
    ),
    "bars_in_regime": _FeatureMeta(
        formula_summary="count consecutive bars staying in the current hard regime",
        runtime_state_inputs=("regimes",),
        live_computable=False,
        compute_scope="runtime_state",
        strategy_roles=("when", "where"),
        promotion_target_default="strategy_helper",
    ),
    "close_in_range": _FeatureMeta(
        formula_summary="(close - low) / (high - low)",
        source_inputs=("bar.high", "bar.low", "bar.close"),
        strategy_roles=("why", "when"),
    ),
    "body_ratio": _FeatureMeta(
        formula_summary="|close - open| / (high - low)",
        source_inputs=("bar.open", "bar.high", "bar.low", "bar.close"),
        strategy_roles=("when",),
    ),
    "upper_wick_ratio": _FeatureMeta(
        formula_summary="(high - max(open,close)) / (high - low)",
        source_inputs=("bar.open", "bar.high", "bar.low", "bar.close"),
        strategy_roles=("why", "where"),
    ),
    "lower_wick_ratio": _FeatureMeta(
        formula_summary="(min(open,close) - low) / (high - low)",
        source_inputs=("bar.open", "bar.high", "bar.low", "bar.close"),
        strategy_roles=("why", "where"),
    ),
    "range_expansion": _FeatureMeta(
        formula_summary="(high - low) / atr14",
        source_inputs=("bar.high", "bar.low", "atr14.atr"),
        strategy_roles=("when",),
    ),
    "oc_imbalance": _FeatureMeta(
        formula_summary="(close - open) / (high - low)",
        source_inputs=("bar.open", "bar.high", "bar.low", "bar.close"),
        strategy_roles=("why",),
    ),
    "session_phase": _FeatureMeta(
        formula_summary="0=Asia / 1=London / 2=NY / 3=Close",
        source_inputs=("bar.time",),
        strategy_roles=("when",),
    ),
    "london_session": _FeatureMeta(
        formula_summary="flag: bar_time.hour ∈ [7, 16) UTC",
        source_inputs=("bar.time",),
        strategy_roles=("when",),
    ),
    "ny_session": _FeatureMeta(
        formula_summary="flag: bar_time.hour ∈ [12, 21) UTC",
        source_inputs=("bar.time",),
        strategy_roles=("when",),
    ),
    "day_progress": _FeatureMeta(
        formula_summary="UTC intraday progress ∈ [0, 1]",
        source_inputs=("bar.time",),
        strategy_roles=("when",),
    ),
    "bars_to_next_high_impact_event": _FeatureMeta(
        formula_summary="minutes to next high-impact event / tf_minutes",
        source_inputs=("bar.time",),
        runtime_state_inputs=("high_impact_event_times",),
        strategy_roles=("when", "why"),
    ),
    "bars_since_last_high_impact_event": _FeatureMeta(
        formula_summary="minutes since last high-impact event / tf_minutes",
        source_inputs=("bar.time",),
        runtime_state_inputs=("high_impact_event_times",),
        strategy_roles=("when",),
    ),
    "in_news_window": _FeatureMeta(
        formula_summary="flag: |bar_time - event| <= 30 minutes",
        source_inputs=("bar.time",),
        runtime_state_inputs=("high_impact_event_times",),
        strategy_roles=("when",),
    ),
    "close_to_close_3": _FeatureMeta(
        formula_summary="(close[i] - close[i-3]) / close[i-3]",
        source_inputs=("bar.close",),
        strategy_roles=("why",),
    ),
    "consecutive_same_color": _FeatureMeta(
        formula_summary="signed count of consecutive same-color bars (capped at 10)",
        source_inputs=("bar.open", "bar.close"),
        strategy_roles=("why",),
    ),
    "child_bar_consensus": _FeatureMeta(
        formula_summary="proportion of child bars with same color as parent bar",
        source_inputs=("child_bars", "bar.open", "bar.close"),
        runtime_state_inputs=("child_bars",),
        live_computable=False,
        strategy_roles=("why",),
    ),
    "child_range_acceleration": _FeatureMeta(
        formula_summary="second_half_avg_range / first_half_avg_range - 1",
        source_inputs=("child_bars",),
        runtime_state_inputs=("child_bars",),
        live_computable=False,
        strategy_roles=("when",),
    ),
    "intrabar_momentum_shift": _FeatureMeta(
        formula_summary="sign changes in child bar close-to-close / total child bars",
        source_inputs=("child_bars",),
        runtime_state_inputs=("child_bars",),
        live_computable=False,
        strategy_roles=("why",),
    ),
    "child_volume_front_weight": _FeatureMeta(
        formula_summary="first_half_volume / total_volume",
        source_inputs=("child_bars",),
        runtime_state_inputs=("child_bars",),
        live_computable=False,
        strategy_roles=("when",),
    ),
    "child_bar_count_ratio": _FeatureMeta(
        formula_summary="actual_child_bars / expected_child_bars (gap detection)",
        source_inputs=("child_bars",),
        runtime_state_inputs=("child_bars",),
        live_computable=False,
        strategy_roles=("where",),
    ),
    # Regime transition features（迁移自 RegimeTransitionProvider）
    "bars_since_change": _FeatureMeta(
        formula_summary="bars since last regime change (0 on change bar)",
        runtime_state_inputs=("regimes",),
        live_computable=False,
        compute_scope="runtime_state",
        strategy_roles=("when",),
    ),
    "dominant_strength": _FeatureMeta(
        formula_summary="max(soft_probs) per bar",
        runtime_state_inputs=("soft_regimes",),
        live_computable=False,
        compute_scope="runtime_state",
        strategy_roles=("why",),
    ),
}


def _get_feature_meta(name: str) -> Optional[_FeatureMeta]:
    """按特征名查询元数据；未注册特征返回 None。"""
    return _FEATURE_META_REGISTRY.get(name)


def _build_registry_inventory() -> Dict[str, Any]:
    """构造注册表摘要（等价于旧 FeatureEngineer.inventory()）。"""
    return {
        "active_features": {
            name: {
                "formula_summary": meta.formula_summary,
                "source_inputs": list(meta.source_inputs),
                "runtime_state_inputs": list(meta.runtime_state_inputs),
                "live_computable": meta.live_computable,
                "compute_scope": meta.compute_scope,
                "bounded_lookback": meta.bounded_lookback,
                "strategy_roles": list(meta.strategy_roles),
                "promotion_target_default": meta.promotion_target_default,
                "no_lookahead": meta.no_lookahead,
                "interpretable": meta.interpretable,
            }
            for name, meta in sorted(_FEATURE_META_REGISTRY.items())
        },
        "promoted_indicator_precedents": list(PROMOTED_INDICATOR_PRECEDENTS),
    }


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
    seed_map = _collect_feature_seed_map(results_by_timeframe)
    candidates: list[FeatureCandidateSpec] = []

    for (feature_name, regime), seed in seed_map.items():
        definition = _get_feature_meta(feature_name)
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
                feature_kind=_infer_feature_kind(definition),
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
        registry_inventory=_build_registry_inventory(),
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
        if item.field_name == feature_name
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
                    f"{best.indicator_name}.{feature_name} IC={best.information_coefficient:+.3f} "
                    f"n={best.n_samples} horizon={best.forward_bars}"
                ),
                direction=("buy" if best.information_coefficient > 0 else "sell"),
                regime=best.regime,
                detail=best.to_dict(),
            )
        )

    for sweep in target_result.threshold_sweeps:
        if sweep.field_name != feature_name:
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
            if sweep.is_significant_buy and sweep.optimal_buy_threshold is not None:
                _register_seed(
                    seed_map,
                    feature_name=sweep.field_name,
                    regime=sweep.regime,
                    timeframe=timeframe,
                    direction="buy",
                    score=max(
                        abs(float(sweep.buy_mean_return)), float(sweep.buy_hit_rate)
                    ),
                    source="threshold_sweep",
                )
            if sweep.is_significant_sell and sweep.optimal_sell_threshold is not None:
                _register_seed(
                    seed_map,
                    feature_name=sweep.field_name,
                    regime=sweep.regime,
                    timeframe=timeframe,
                    direction="sell",
                    score=max(
                        abs(float(sweep.sell_mean_return)), float(sweep.sell_hit_rate)
                    ),
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
        for direction in (_resolve_seed_direction(entry) for entry in seed.values())
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


def _infer_feature_kind(meta: _FeatureMeta) -> FeatureKind:
    """根据特征元数据自动推断 derived / computed 类型（ADR-007）。

    DERIVED（组合型）：bar_close scope + live_computable + 无 runtime_state 依赖
                       → 策略可通过现有指标字段直接引用，无需晋升
    COMPUTED（计算型）：任一条件不满足
                       → 需独立 Python 函数，晋升需 4 步手工
    """
    if meta.runtime_state_inputs:
        return FeatureKind.COMPUTED
    if not meta.live_computable:
        return FeatureKind.COMPUTED
    if meta.compute_scope != "bar_close":
        return FeatureKind.COMPUTED
    return FeatureKind.DERIVED


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
