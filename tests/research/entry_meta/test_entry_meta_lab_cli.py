from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

from src.ops.cli import entry_meta_lab
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
    assert "--feature-scope" in completed.stdout


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


def test_entry_meta_lab_stdout_uses_wrapped_payload(monkeypatch, capsys, tmp_path) -> None:
    import src.backtesting.component_factory as component_factory
    import src.config.instance_context as instance_context
    import src.ops.cli._coverage as coverage_module
    import src.research.core.backends as backends
    import src.research.core.config as research_config
    import src.research.entry_meta.lab as lab_module

    result_payload = {
        "artifact_path": "artifacts/entry_meta_artifact.json",
        "dataset_summary": {"matched_trades": 8},
        "quality": {"status": "accepted"},
        "metrics": {"auc": 0.73},
    }
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
    run_calls: list[dict[str, object]] = []

    class FakeDeps:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

    class FakeEntryMetaLab:
        def __init__(self, *, config, deps) -> None:
            self.config = config
            self.deps = deps

        def run(self, **kwargs):
            run_calls.append(kwargs)
            return SimpleNamespace(to_dict=lambda: result_payload)

    monkeypatch.setattr(sys, "argv", [
        "entry_meta_lab.py",
        "--environment",
        "demo",
        "--baseline",
        str(tmp_path / "baseline.json"),
        "--tf",
        "h1",
        "--start",
        "2026-01-01T00:00:00Z",
        "--end",
        "2026-01-31T23:00:00Z",
        "--backend",
        "cpu",
        "--artifact-dir",
        str(tmp_path / "artifacts"),
        "--symbol",
        "XAUUSD",
        "--feature-scope",
        "research_full",
    ])
    monkeypatch.setattr(instance_context, "set_current_environment", lambda environment: None)
    monkeypatch.setattr(
        backends,
        "resolve_backend",
        lambda backend_name: SimpleNamespace(
            name=backend_name,
            assert_available=lambda: None,
        ),
    )
    monkeypatch.setattr(
        coverage_module,
        "ensure_ohlc_data_coverage",
        lambda **kwargs: coverage,
    )
    monkeypatch.setattr(component_factory, "build_research_data_deps", lambda: FakeDeps())
    monkeypatch.setattr(research_config, "load_research_config", lambda: object())
    monkeypatch.setattr(lab_module, "EntryMetaLab", FakeEntryMetaLab)

    entry_meta_lab.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["symbol"] == "XAUUSD"
    assert payload["environment"] == "demo"
    assert payload["backend"] == "cpu"
    assert payload["coverage"] == {
        "H1": {
            "timeframe": "H1",
            "count": 120,
            "first": "2026-01-01T00:00:00+00:00",
            "last": "2026-01-31T23:00:00+00:00",
        }
    }
    assert payload["result"]["artifact_path"] == result_payload["artifact_path"]
    assert payload["result"]["dataset_summary"] == result_payload["dataset_summary"]
    assert payload["result"]["quality"] == result_payload["quality"]
    assert payload["result"]["metrics"] == result_payload["metrics"]
    assert run_calls[0]["feature_scope"] == "research_full"
