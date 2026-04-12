from __future__ import annotations

from typing import Any, Iterable, Optional

from ..core.contracts import (
    FeatureCandidateSpec,
    FeaturePromotionReport,
    IndicatorPromotionDecision,
)


def build_feature_promotion_report(
    candidate: FeatureCandidateSpec,
    *,
    promoted_indicator_name: Optional[str],
    strategy_candidates: Iterable[str] = (),
    validation_summary: Optional[dict[str, Any]] = None,
    promotion_decision: IndicatorPromotionDecision | None = None,
) -> FeaturePromotionReport:
    decision = promotion_decision or candidate.promotion_decision
    lineage = {
        "research_feature_name": candidate.feature_name,
        "promoted_indicator_name": promoted_indicator_name,
        "downstream_strategy_candidates": list(strategy_candidates),
        "research_provenance": candidate.research_provenance,
    }
    return FeaturePromotionReport(
        candidate_id=candidate.candidate_id,
        feature_name=candidate.feature_name,
        promoted_indicator_name=promoted_indicator_name,
        promotion_decision=decision,
        strategy_candidates=tuple(strategy_candidates),
        validation_summary=dict(validation_summary or {}),
        research_provenance=candidate.research_provenance,
        lineage=lineage,
    )
