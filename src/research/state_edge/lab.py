from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.research.core.config import ResearchConfig, load_research_config
from src.research.core.data_matrix import build_data_matrix
from src.research.core.ports import ResearchDataDeps
from src.research.features.hub import FeatureHub
from src.research.state_edge.artifacts import StateEdgeArtifact, save_artifact
from src.research.state_edge.backends import resolve_backend
from src.research.state_edge.sequence_training import train_sequence_tcn_artifact
from src.research.state_edge.sequence_training import train_sequence_mlp_artifact
from src.research.state_edge.training import train_state_edge_artifact
from src.utils.timezone import utc_now


@dataclass(frozen=True)
class StateEdgeLabResult:
    artifact: StateEdgeArtifact
    artifact_path: Path
    backend_diagnostics: dict[str, Any]
    feature_compute_summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.artifact.model_id,
            "symbol": self.artifact.symbol,
            "timeframe": self.artifact.timeframe,
            "status": self.artifact.status,
            "artifact_path": str(self.artifact_path),
            "backend": self.artifact.backend,
            "backend_diagnostics": self.backend_diagnostics,
            "label_summary": self.artifact.label_summary,
            "metrics": self.artifact.metrics,
            "feature_compute_summary": self.feature_compute_summary,
            "feature_manifest": self.artifact.feature_manifest,
        }


class StateEdgeLab:
    """离线 State Edge 训练编排器。

    职责边界：ResearchDataDeps -> DataMatrix -> FeatureHub -> state-edge artifact。
    不持有 SignalModule，不修改 demo/live runtime。
    """

    def __init__(
        self,
        *,
        deps: ResearchDataDeps,
        config: ResearchConfig | None = None,
        backend_name: str = "cpu",
        artifact_dir: str | Path = "artifacts/state_edge",
        model_kind: str | None = None,
        sequence_window: int | None = None,
        epochs: int | None = None,
        batch_size: int | None = None,
    ) -> None:
        self._deps = deps
        self._config = config or load_research_config()
        self._backend_name = backend_name
        self._artifact_dir = Path(artifact_dir)
        self._model_kind = model_kind or self._config.state_edge_model.model_kind
        self._sequence_window = (
            sequence_window or self._config.state_edge_model.sequence_window
        )
        self._epochs = epochs or self._config.state_edge_model.sequence_epochs
        self._batch_size = batch_size or self._config.state_edge_model.sequence_batch_size
        self._feature_hub = FeatureHub(self._config)

    def run(
        self,
        *,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
    ) -> StateEdgeLabResult:
        backend = resolve_backend(self._backend_name)
        backend_diagnostics = backend.diagnostics()
        backend.assert_available()

        extra_reqs = self._feature_hub.required_extra_data()
        if extra_reqs:
            raise ValueError(
                "StateEdgeLab phase 1 does not support cross-TF extra data yet; "
                "disable cross_tf provider in research.ini for this run."
            )

        matrix = build_data_matrix(
            symbol=symbol,
            timeframe=timeframe,
            start_time=start_time,
            end_time=end_time,
            forward_horizons=self._config.forward_horizons,
            warmup_bars=self._config.warmup_bars,
            train_ratio=self._config.train_ratio,
            round_trip_cost_pct=self._config.round_trip_cost_pct,
            deps=self._deps,
        )
        feature_summary = self._feature_hub.compute_all(matrix).to_dict()
        model_id = f"state-edge-{timeframe}-{utc_now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        if self._model_kind == "sequence_tcn":
            artifact = train_sequence_tcn_artifact(
                matrix,
                backend_name=self._backend_name,
                model_id=model_id,
                sequence_window=self._sequence_window,
                epochs=self._epochs,
                batch_size=self._batch_size,
                learning_rate=self._config.state_edge_model.sequence_learning_rate,
            )
        elif self._model_kind == "sequence_mlp":
            artifact = train_sequence_mlp_artifact(
                matrix,
                backend_name=self._backend_name,
                model_id=model_id,
                sequence_window=self._sequence_window,
                epochs=self._epochs,
                batch_size=self._batch_size,
                learning_rate=self._config.state_edge_model.sequence_learning_rate,
            )
        elif self._model_kind == "tabular" or self._model_kind == "hist_gradient_boosting":
            artifact = train_state_edge_artifact(
                matrix,
                backend_name=self._backend_name,
                model_id=model_id,
                top_bucket_quantile=self._config.state_edge_model.top_bucket_quantile,
            )
        else:
            raise ValueError(f"Unsupported State Edge model_kind: {self._model_kind}")
        artifact_path = save_artifact(artifact, self._artifact_dir / model_id)
        return StateEdgeLabResult(
            artifact=artifact,
            artifact_path=artifact_path,
            backend_diagnostics=backend_diagnostics,
            feature_compute_summary=feature_summary,
        )


def train_state_edge_artifact_for_matrix(
    matrix: Any,
    *,
    backend_name: str = "cpu",
    model_id: str | None = None,
) -> StateEdgeArtifact:
    """测试与脚本复用的薄包装，避免 CLI 直接依赖训练细节。"""

    return train_state_edge_artifact(
        matrix,
        backend_name=backend_name,
        model_id=model_id,
    )
