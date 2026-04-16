"""Research feature / indicator candidate 路径。"""

from src.research.features.candidates import discover_feature_candidates
from src.research.features.hub import FeatureHub
from src.research.features.promotion import build_feature_promotion_report
from src.research.features.protocol import (
    PROMOTED_INDICATOR_PRECEDENTS,
    FeatureComputeResult,
    FeatureProvider,
    FeatureRole,
    ProviderDataRequirement,
)

__all__ = [
    "FeatureComputeResult",
    "FeatureHub",
    "FeatureProvider",
    "FeatureRole",
    "PROMOTED_INDICATOR_PRECEDENTS",
    "ProviderDataRequirement",
    "build_feature_promotion_report",
    "discover_feature_candidates",
]
