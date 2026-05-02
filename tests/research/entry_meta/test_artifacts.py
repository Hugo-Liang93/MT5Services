from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.research.entry_meta.artifacts import (
    EntryMetaArtifact,
    EntryMetaPrediction,
    load_artifact,
    save_artifact,
)


def test_entry_meta_artifact_roundtrip(tmp_path: Path) -> None:
    artifact = EntryMetaArtifact(
        model_id="entry-meta-H1-test",
        symbol="XAUUSD",
        timeframe="H1",
        backend="cpu",
        model_kind="tabular",
        feature_keys=["entry.confidence"],
        label_summary={"take_entry": 1, "block_entry": 1},
        sample_weight_summary={"min": 1.0, "max": 2.0, "mean": 1.5},
        metrics={"oos_accuracy": 0.5},
        predictions=[
            EntryMetaPrediction(
                bar_time="2026-01-01T01:00:00+00:00",
                strategy="s",
                direction="buy",
                take_entry_prob=0.7,
                block_entry_prob=0.3,
                threshold_context={"min_take_prob": 0.6},
            )
        ],
        model_payload={"kind": "constant", "take_entry_prob": 0.7},
        feature_manifest={"source": "test"},
    )

    path = save_artifact(artifact, tmp_path / "artifact-dir")

    loaded = load_artifact(path)
    assert loaded == artifact
    assert path.name == "entry_meta_artifact.json"

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["artifact_type"] == "entry_meta"
    assert payload["status"] == "trained"
    assert payload["predictions"][0]["threshold_context"] == {"min_take_prob": 0.6}


def test_entry_meta_artifact_preserves_structured_metrics() -> None:
    payload = {
        "model_id": "entry-meta-H1-test",
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "backend": "cpu",
        "model_kind": "tabular",
        "feature_keys": [],
        "label_summary": {"take_entry": 1, "block_entry": 0},
        "sample_weight_summary": {"min": 1.0, "max": 1.0, "mean": 1.0},
        "metrics": {"oos": {"accuracy": 0.5}, "status": "ok"},
        "predictions": [],
        "model_payload": {"kind": "constant"},
        "feature_manifest": {},
    }

    artifact = EntryMetaArtifact.from_dict(payload)

    assert artifact.metrics == {"oos": {"accuracy": 0.5}, "status": "ok"}
    assert artifact.to_dict()["metrics"] == {"oos": {"accuracy": 0.5}, "status": "ok"}


def test_load_artifact_accepts_directory_path(tmp_path: Path) -> None:
    artifact = EntryMetaArtifact(
        model_id="entry-meta-M15-test",
        symbol="XAUUSD",
        timeframe="M15",
        backend="cpu",
        model_kind="tabular",
        feature_keys=[],
        label_summary={"take_entry": 0, "block_entry": 1},
        sample_weight_summary={"min": 1.0, "max": 1.0, "mean": 1.0},
        metrics={},
        predictions=[],
        model_payload={"kind": "constant", "take_entry_prob": 0.0},
        feature_manifest={},
    )
    artifact_dir = tmp_path / "artifact-dir"

    save_artifact(artifact, artifact_dir)

    assert load_artifact(artifact_dir) == artifact


def test_load_artifact_rejects_non_object_json(tmp_path: Path) -> None:
    path = tmp_path / "entry_meta_artifact.json"
    path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must be an object"):
        load_artifact(path)


def test_from_dict_defaults_status_to_trained_and_validates_artifact_type() -> None:
    payload = {
        "model_id": "entry-meta-H1-test",
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "backend": "cpu",
        "model_kind": "tabular",
        "feature_keys": [],
        "label_summary": {"take_entry": 1, "block_entry": 0},
        "sample_weight_summary": {"min": 1.0, "max": 1.0, "mean": 1.0},
        "metrics": {},
        "predictions": [
            {
                "bar_time": "2026-01-01T01:00:00+00:00",
                "strategy": "s",
                "direction": "buy",
                "take_entry_prob": 0.7,
                "block_entry_prob": 0.3,
            }
        ],
        "model_payload": {"kind": "constant"},
        "feature_manifest": {},
    }

    artifact = EntryMetaArtifact.from_dict(payload)

    assert artifact.status == "trained"
    assert artifact.predictions[0].threshold_context is None

    with pytest.raises(ValueError, match="artifact_type"):
        EntryMetaArtifact.from_dict({**payload, "artifact_type": "other"})


def test_from_dict_rejects_non_object_prediction_items() -> None:
    payload = {
        "model_id": "entry-meta-H1-test",
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "backend": "cpu",
        "model_kind": "tabular",
        "feature_keys": [],
        "label_summary": {"take_entry": 1, "block_entry": 0},
        "sample_weight_summary": {"min": 1.0, "max": 1.0, "mean": 1.0},
        "metrics": {},
        "predictions": ["bad"],
        "model_payload": {"kind": "constant"},
        "feature_manifest": {},
    }

    with pytest.raises(ValueError, match="prediction.*object"):
        EntryMetaArtifact.from_dict(payload)


def test_prediction_rejects_non_object_threshold_context() -> None:
    payload = {
        "bar_time": "2026-01-01T01:00:00+00:00",
        "strategy": "s",
        "direction": "buy",
        "take_entry_prob": 0.7,
        "block_entry_prob": 0.3,
        "threshold_context": "bad",
    }

    with pytest.raises(ValueError, match="threshold_context"):
        EntryMetaPrediction.from_dict(payload)
