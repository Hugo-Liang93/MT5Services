from __future__ import annotations

from src.research.state_edge.artifacts import (
    StateEdgeArtifact,
    StateEdgePrediction,
    load_artifact,
    save_artifact,
)


def test_artifact_round_trip_is_stable(tmp_path) -> None:
    artifact = StateEdgeArtifact(
        model_id="state-edge-H1-test",
        symbol="XAUUSD",
        timeframe="H1",
        backend="cpu",
        feature_keys=["indicator.rsi14.rsi", "regime.hard_code"],
        label_summary={"long": 10, "short": 8, "no_trade": 12},
        metrics={"oos_accuracy": 0.42},
        predictions=[
            StateEdgePrediction(
                bar_time="2026-01-01T00:00:00+00:00",
                long_edge_prob=0.7,
                short_edge_prob=0.1,
                no_trade_prob=0.2,
            )
        ],
        model_payload={"kind": "constant", "class_probs": [0.3, 0.2, 0.5]},
    )

    written = save_artifact(artifact, tmp_path / "artifact")
    loaded = load_artifact(written)

    assert loaded == artifact
    assert load_artifact(tmp_path / "artifact") == artifact
