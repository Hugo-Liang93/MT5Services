from __future__ import annotations

from pathlib import Path


def _runner_source() -> str:
    repo_root = Path(__file__).resolve().parents[2]
    return (
        repo_root / "src" / "ops" / "cli" / "walkforward_runner.py"
    ).read_text(encoding="utf-8")


def test_walkforward_runner_uses_current_split_field_names() -> None:
    src = _runner_source()
    assert "split.in_sample_result.metrics" in src
    assert "split.out_of_sample_result.metrics" in src
    assert "split.is_result" not in src
    assert "split.oos_result" not in src


def test_walkforward_runner_exports_split_details_to_json() -> None:
    src = _runner_source()
    assert '"splits": [' in src
    assert '"best_params": dict(split.best_params)' in src
