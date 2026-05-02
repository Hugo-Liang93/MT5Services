from __future__ import annotations

from pathlib import Path

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
            )
        ],
        model_payload={"kind": "constant", "take_entry_prob": 0.7},
        feature_manifest={"source": "test"},
        status="trained",
    )

    path = save_artifact(artifact, tmp_path / "artifact-dir")

    loaded = load_artifact(path)
    assert loaded == artifact
    assert path.name == "entry_meta_artifact.json"
