from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_state_edge_sequence_eval_cli_help() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "src.ops.cli.state_edge_sequence_eval", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "--model-kind" in completed.stdout
    assert "--threshold-grid" in completed.stdout
    assert "--include-demo-validation" in completed.stdout
    assert "--skip-artifact-quality-gate" in completed.stdout
    assert "--artifact-quality-only" in completed.stdout
    assert "--state-edge-directions" in completed.stdout


def test_evaluate_tf_emits_incremental_progress(monkeypatch) -> None:
    from src.ops.cli import state_edge_sequence_eval as cli

    progress: list[dict] = []

    monkeypatch.setattr(
        cli,
        "_train_artifact",
        lambda **_: {
            "artifact_path": str(Path("artifacts") / "state_edge_artifact.json"),
            "label_summary": {"long": 10, "short": 8, "no_trade": 2},
            "status": "trained",
            "metrics": {
                "oos_samples": 60,
                "top_probability_buckets": {
                    "long": {
                        "sample_count": 8,
                        "mean_cost_after_return": 0.01,
                        "hit_rate_lift": 0.1,
                    },
                    "short": {
                        "sample_count": 9,
                        "mean_cost_after_return": 0.01,
                        "hit_rate_lift": 0.1,
                    },
                },
            },
        },
    )

    def fake_run_backtest(**kwargs):
        threshold = kwargs.get("threshold", 0.50)
        artifact_path = kwargs.get("artifact_path")
        mode = kwargs.get("mode")
        if artifact_path is None:
            return {
                "metrics": {"pf": 1.0, "expectancy": 1.0, "max_dd": 5.0, "trades": 40}
            }
        if mode == "shadow":
            return {
                "metrics": {"pf": 1.0, "expectancy": 1.0, "max_dd": 5.0, "trades": 40}
            }
        return {
            "threshold": threshold,
            "metrics": {
                "pf": 1.2 + threshold,
                "expectancy": 1.1,
                "max_dd": 5.1,
                "trades": 30,
            },
        }

    monkeypatch.setattr(cli, "_run_backtest", fake_run_backtest)

    result = cli._evaluate_tf(
        tf="H1",
        start="2026-03-01",
        end="2026-03-15",
        backend="gpu",
        model_kind="sequence_mlp",
        sequence_window=32,
        epochs=2,
        batch_size=512,
        artifact_dir="artifacts/state_edge_sequence_eval_test",
        thresholds=[0.50, 0.55],
        include_demo_validation=True,
        min_oos_samples=30,
        min_top_bucket_samples=5,
        min_label_class_samples=2,
        on_progress=lambda payload: progress.append(dict(payload)),
    )

    assert [item["stage"] for item in progress] == [
        "artifact",
        "artifact_quality",
        "baseline",
        "shadow",
        "filter:0.5",
        "filter:0.55",
        "threshold_report",
    ]
    assert result["threshold_report"]["status"] == "accepted"


def test_evaluate_tf_skips_backtests_when_artifact_quality_fails(monkeypatch) -> None:
    from src.ops.cli import state_edge_sequence_eval as cli

    progress: list[dict] = []

    monkeypatch.setattr(
        cli,
        "_train_artifact",
        lambda **_: {
            "artifact_path": str(Path("artifacts") / "state_edge_artifact.json"),
            "status": "trained",
            "label_summary": {"long": 67, "short": 153, "no_trade": 10},
            "metrics": {
                "oos_samples": 1,
                "top_probability_buckets": {
                    "long": {"sample_count": 0},
                    "short": {"sample_count": 0},
                },
            },
        },
    )

    def fail_backtest(**_):
        raise AssertionError("quality gate should skip expensive backtests")

    monkeypatch.setattr(cli, "_run_backtest", fail_backtest)

    result = cli._evaluate_tf(
        tf="H1",
        start="2026-03-01",
        end="2026-03-15",
        backend="gpu",
        model_kind="sequence_mlp",
        sequence_window=32,
        epochs=2,
        batch_size=512,
        artifact_dir="artifacts/state_edge_sequence_eval_test",
        thresholds=[0.50],
        include_demo_validation=True,
        min_oos_samples=30,
        min_top_bucket_samples=5,
        min_label_class_samples=10,
        on_progress=lambda payload: progress.append(dict(payload)),
    )

    assert result["status"] == "refit"
    assert result["artifact_quality"]["should_run_backtest"] is False
    assert [item["stage"] for item in progress] == ["artifact", "artifact_quality"]


def test_evaluate_tf_can_stop_after_quality_gate_even_when_quality_passes(
    monkeypatch,
) -> None:
    from src.ops.cli import state_edge_sequence_eval as cli

    progress: list[dict] = []

    monkeypatch.setattr(
        cli,
        "_train_artifact",
        lambda **_: {
            "artifact_path": str(Path("artifacts") / "state_edge_artifact.json"),
            "status": "trained",
            "label_summary": {"long": 80, "short": 90, "no_trade": 30},
            "metrics": {
                "oos_samples": 120,
                "top_probability_buckets": {
                    "long": {
                        "sample_count": 10,
                        "mean_cost_after_return": 0.01,
                        "hit_rate_lift": 0.1,
                    },
                    "short": {
                        "sample_count": 12,
                        "mean_cost_after_return": 0.01,
                        "hit_rate_lift": 0.1,
                    },
                },
            },
        },
    )

    def fail_backtest(**_):
        raise AssertionError("artifact_quality_only should not run backtests")

    monkeypatch.setattr(cli, "_run_backtest", fail_backtest)

    result = cli._evaluate_tf(
        tf="H1",
        start="2026-01-01",
        end="2026-04-15",
        backend="gpu",
        model_kind="sequence_mlp",
        sequence_window=64,
        epochs=8,
        batch_size=512,
        artifact_dir="artifacts/state_edge_sequence_eval_test",
        thresholds=[0.50],
        include_demo_validation=True,
        min_oos_samples=30,
        min_top_bucket_samples=5,
        min_label_class_samples=10,
        artifact_quality_only=True,
        on_progress=lambda payload: progress.append(dict(payload)),
    )

    assert result["status"] == "accepted"
    assert result["artifact_quality"]["should_run_backtest"] is True
    assert [item["stage"] for item in progress] == ["artifact", "artifact_quality"]
