from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ARTIFACT_FILENAME = "entry_meta_artifact.json"
ARTIFACT_TYPE = "entry_meta"


@dataclass(frozen=True)
class EntryMetaPrediction:
    bar_time: str
    strategy: str
    direction: str
    take_entry_prob: float
    block_entry_prob: float
    threshold_context: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bar_time": self.bar_time,
            "strategy": self.strategy,
            "direction": self.direction,
            "take_entry_prob": self.take_entry_prob,
            "block_entry_prob": self.block_entry_prob,
            "threshold_context": self.threshold_context,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EntryMetaPrediction:
        if not isinstance(data, dict):
            raise ValueError("entry meta artifact prediction must be an object")
        _require_keys(
            data,
            {
                "bar_time",
                "strategy",
                "direction",
                "take_entry_prob",
                "block_entry_prob",
            },
        )
        threshold_context = data.get("threshold_context")
        if threshold_context is not None and not isinstance(threshold_context, dict):
            raise ValueError("entry meta artifact threshold_context must be an object")
        return cls(
            bar_time=str(data["bar_time"]),
            strategy=str(data["strategy"]),
            direction=str(data["direction"]),
            take_entry_prob=float(data["take_entry_prob"]),
            block_entry_prob=float(data["block_entry_prob"]),
            threshold_context=dict(threshold_context) if threshold_context is not None else None,
        )


@dataclass(frozen=True)
class EntryMetaArtifact:
    model_id: str
    symbol: str
    timeframe: str
    backend: str
    model_kind: str
    feature_keys: list[str]
    label_summary: dict[str, int]
    sample_weight_summary: dict[str, float]
    metrics: dict[str, Any]
    predictions: list[EntryMetaPrediction]
    model_payload: dict[str, Any]
    feature_manifest: dict[str, Any]
    status: str = "trained"

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": ARTIFACT_TYPE,
            "model_id": self.model_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "backend": self.backend,
            "model_kind": self.model_kind,
            "feature_keys": list(self.feature_keys),
            "label_summary": dict(self.label_summary),
            "sample_weight_summary": dict(self.sample_weight_summary),
            "metrics": dict(self.metrics),
            "predictions": [prediction.to_dict() for prediction in self.predictions],
            "model_payload": dict(self.model_payload),
            "feature_manifest": dict(self.feature_manifest),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EntryMetaArtifact:
        artifact_type = data.get("artifact_type")
        if artifact_type is not None and artifact_type != ARTIFACT_TYPE:
            raise ValueError(f"entry meta artifact has unsupported artifact_type: {artifact_type}")
        _require_keys(
            data,
            {
                "model_id",
                "symbol",
                "timeframe",
                "backend",
                "model_kind",
                "feature_keys",
                "label_summary",
                "sample_weight_summary",
                "metrics",
                "predictions",
                "model_payload",
                "feature_manifest",
            },
        )
        predictions = data["predictions"]
        if not isinstance(predictions, list):
            raise ValueError("entry meta artifact predictions must be a list")
        for prediction in predictions:
            if not isinstance(prediction, dict):
                raise ValueError("entry meta artifact prediction must be an object")
        if not isinstance(data["model_payload"], dict):
            raise ValueError("entry meta artifact model_payload must be an object")
        return cls(
            model_id=str(data["model_id"]),
            symbol=str(data["symbol"]),
            timeframe=str(data["timeframe"]),
            backend=str(data["backend"]),
            model_kind=str(data["model_kind"]),
            feature_keys=[str(item) for item in data["feature_keys"]],
            label_summary={str(key): int(value) for key, value in data["label_summary"].items()},
            sample_weight_summary={
                str(key): float(value) for key, value in data["sample_weight_summary"].items()
            },
            metrics=dict(data["metrics"]),
            predictions=[EntryMetaPrediction.from_dict(item) for item in predictions],
            model_payload=dict(data["model_payload"]),
            feature_manifest=dict(data["feature_manifest"]),
            status=str(data.get("status", "trained")),
        )


def save_artifact(artifact: EntryMetaArtifact, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / ARTIFACT_FILENAME
    tmp_path = path.with_name(f"{path.name}.tmp")
    payload = json.dumps(artifact.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    tmp_path.write_text(f"{payload}\n", encoding="utf-8")
    tmp_path.replace(path)
    return path


def load_artifact(path: Path) -> EntryMetaArtifact:
    if path.is_dir():
        path = path / ARTIFACT_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("entry meta artifact JSON must be an object")
    return EntryMetaArtifact.from_dict(data)


def _require_keys(data: dict[str, Any], required: set[str]) -> None:
    missing = sorted(required - data.keys())
    if missing:
        raise ValueError(f"entry meta artifact missing required fields: {', '.join(missing)}")
