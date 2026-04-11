from __future__ import annotations

from pathlib import Path

from src.entrypoint.web import _resolve_log_dir


def test_resolve_log_dir_relative_to_project_root() -> None:
    resolved = _resolve_log_dir("data/logs")

    assert resolved == Path(__file__).resolve().parents[2] / "data" / "logs"
