"""研究模块公共入口。

包边界：
  - core: 公共基础能力与契约（含 ports.py — 研究域数据依赖端口）
  - analyzers: 共享统计证据引擎
  - features: research feature / indicator candidate 路径
  - strategies: strategy candidate 路径
  - orchestration: MiningRunner 编排入口
  - nightly: **跨域编排工具**（不属于研究核心域）

核心域 vs 编排层分层（P4, 2026-04-22 落地）：
  - 研究**核心域** = core + analyzers + features + strategies + orchestration
    禁止直接 import src.backtesting。数据依赖通过 ResearchDataDeps 端口注入
    （装配层在 CLI / API 中调用 `backtesting.component_factory.build_research_data_deps`
    构造 deps 再注入 MiningRunner）。
  - **编排层** = nightly/ 子包，定位等同 src/ops/cli/：它合法地把 research 与
    backtesting 粘起来触发定期回测。为降低误解，未来 nightly 内容继续膨胀时
    可迁移到 src/ops/research_nightly/。

grep 门禁：`grep "from src.backtesting" src/research/{core,analyzers,features,strategies,orchestration}`
必须 0 命中；nightly/ 除外。
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
