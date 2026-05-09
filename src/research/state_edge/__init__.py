"""State Edge Research Lab.

离线训练市场状态概率模型，输出 long/short/no-trade 三分类概率。
第一阶段仅供 research 与 backtest overlay 使用，不接入 demo/live runtime。
"""

from .artifacts import (
    StateEdgeArtifact,
    StateEdgePrediction,
    load_artifact,
    save_artifact,
)
from .backends import BackendUnavailableError, ComputeBackend, resolve_backend
from .evaluation import (
    StateEdgeEvaluationDecision,
    ThresholdGridReport,
    build_threshold_grid_report,
    evaluate_overlay_increment,
)
from .features import StateEdgeFeatureBuilder, StateEdgeFeatureMatrix
from .lab import StateEdgeLab, StateEdgeLabResult, train_state_edge_artifact_for_matrix
from .labels import StateEdgeClass, StateEdgeLabelBuilder, StateEdgeLabelSet
from .quality import StateEdgeArtifactQualityReport, evaluate_artifact_quality
from .sequence import SequenceWindowBuilder, SequenceWindowMatrix
from .sequence_training import train_sequence_mlp_artifact, train_sequence_tcn_artifact
from .training import train_state_edge_artifact

__all__ = [
    "BackendUnavailableError",
    "ComputeBackend",
    "StateEdgeArtifact",
    "StateEdgeArtifactQualityReport",
    "StateEdgeClass",
    "StateEdgeFeatureBuilder",
    "StateEdgeFeatureMatrix",
    "StateEdgeEvaluationDecision",
    "StateEdgeLab",
    "StateEdgeLabResult",
    "StateEdgeLabelBuilder",
    "StateEdgeLabelSet",
    "StateEdgePrediction",
    "SequenceWindowBuilder",
    "SequenceWindowMatrix",
    "ThresholdGridReport",
    "build_threshold_grid_report",
    "evaluate_overlay_increment",
    "evaluate_artifact_quality",
    "load_artifact",
    "resolve_backend",
    "save_artifact",
    "train_sequence_tcn_artifact",
    "train_sequence_mlp_artifact",
    "train_state_edge_artifact",
    "train_state_edge_artifact_for_matrix",
]
