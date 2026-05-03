from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StateEdgePrediction:
    bar_time: str
    long_edge_prob: float
    short_edge_prob: float
    no_trade_prob: float


@dataclass(frozen=True)
class StateEdgeArtifact:
    model_id: str
    symbol: str
    timeframe: str
    backend: str
    feature_keys: list[str]
    label_summary: dict[str, int]
    metrics: dict[str, Any]
    predictions: list[StateEdgePrediction]
    model_payload: dict[str, Any] = field(default_factory=dict)
    feature_manifest: dict[str, Any] = field(default_factory=dict)
    status: str = "trained"


def save_artifact(artifact: StateEdgeArtifact, path: str | Path) -> Path:
    target = Path(path)
    if target.suffix.lower() != ".json":
        target.mkdir(parents=True, exist_ok=True)
        target = target / "state_edge_artifact.json"
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(artifact)
    with target.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
    return target


def load_artifact(path: str | Path) -> StateEdgeArtifact:
    source = Path(path)
    if source.is_dir():
        source = source / "state_edge_artifact.json"
    with source.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    predictions = [
        StateEdgePrediction(**item) for item in payload.get("predictions", [])
    ]
    return StateEdgeArtifact(
        model_id=payload["model_id"],
        symbol=payload["symbol"],
        timeframe=payload["timeframe"],
        backend=payload["backend"],
        feature_keys=list(payload.get("feature_keys", [])),
        label_summary=dict(payload.get("label_summary", {})),
        metrics=dict(payload.get("metrics", {})),
        predictions=predictions,
        model_payload=dict(payload.get("model_payload", {})),
        feature_manifest=dict(payload.get("feature_manifest", {})),
        status=str(payload.get("status", "trained")),
    )
