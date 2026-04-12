"""研究模块公共入口。

包边界：
  - core: 公共基础能力与契约
  - analyzers: 共享统计证据引擎
  - features: research feature / indicator candidate 路径
  - strategies: strategy candidate 路径
  - orchestration: MiningRunner 编排入口
"""

from src.research.core.contracts import (
    CandidateDiscoveryResult,
    FeatureCandidateDiscoveryResult,
    FeatureCandidateSpec,
    FeaturePromotionReport,
    IndicatorPromotionDecision,
    PromotionDecision,
    RobustnessTier,
    StrategyCandidateSpec,
)
from src.research.features.candidates import discover_feature_candidates
from src.research.features.promotion import build_feature_promotion_report
from src.research.orchestration.runner import MiningRunner
from src.research.strategies.candidates import discover_strategy_candidates

__all__ = [
    "CandidateDiscoveryResult",
    "FeatureCandidateDiscoveryResult",
    "FeatureCandidateSpec",
    "FeaturePromotionReport",
    "IndicatorPromotionDecision",
    "build_feature_promotion_report",
    "discover_feature_candidates",
    "discover_strategy_candidates",
    "MiningRunner",
    "PromotionDecision",
    "RobustnessTier",
    "StrategyCandidateSpec",
]
