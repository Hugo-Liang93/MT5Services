from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

from src.ops.cli.entry_meta_lab import build_entry_meta_lab_output_payload


def test_entry_meta_lab_help_exposes_required_options() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "src.ops.cli.entry_meta_lab", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--baseline" in completed.stdout
    assert "--tf" in completed.stdout
    assert "--backend" in completed.stdout
    assert "--json-output" in completed.stdout


def test_build_entry_meta_lab_output_payload_wraps_result_and_coverage() -> None:
    result = SimpleNamespace(
        to_dict=lambda: {
            "artifact_path": "artifacts/entry_meta_artifact.json",
            "dataset_summary": {"matched_trades": 8},
            "quality": {"status": "accepted"},
            "metrics": {"auc": 0.73},
        }
    )
    coverage = {
        "H1": SimpleNamespace(
            to_dict=lambda: {
                "timeframe": "H1",
                "count": 120,
                "first": "2026-01-01T00:00:00+00:00",
                "last": "2026-01-31T23:00:00+00:00",
            }
        )
    }

    payload = build_entry_meta_lab_output_payload(
        symbol="XAUUSD",
        environment="demo",
        backend_name="cpu",
        coverage=coverage,
        result=result,
    )

    assert payload == {
        "symbol": "XAUUSD",
        "environment": "demo",
        "backend": "cpu",
        "coverage": {
            "H1": {
                "timeframe": "H1",
                "count": 120,
                "first": "2026-01-01T00:00:00+00:00",
                "last": "2026-01-31T23:00:00+00:00",
            }
        },
        "result": {
            "artifact_path": "artifacts/entry_meta_artifact.json",
            "dataset_summary": {"matched_trades": 8},
            "quality": {"status": "accepted"},
            "metrics": {"auc": 0.73},
        },
    }
