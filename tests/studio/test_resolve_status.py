"""Tests for resolve_status helper in src/studio/mappers.py."""
from __future__ import annotations

from src.studio.mappers import resolve_status


def test_returns_first_truthy_check() -> None:
    result = resolve_status("test_agent", [
        (False, "a", "task_a", "none"),
        (True, "b", "task_b", "warning"),
        (True, "c", "task_c", "error"),
    ], "default", "default_task")

    assert result["status"] == "b"
    assert result["task"] == "task_b"
    assert result["alertLevel"] == "warning"


def test_returns_default_when_no_checks_match() -> None:
    result = resolve_status("test_agent", [
        (False, "a", "task_a", "error"),
        (False, "b", "task_b", "warning"),
    ], "idle", "等待中")

    assert result["status"] == "idle"
    assert result["task"] == "等待中"
    assert result["alertLevel"] == "none"


def test_returns_default_with_empty_checks() -> None:
    result = resolve_status("test_agent", [], "default", "default_task")
    assert result["status"] == "default"


def test_passes_metrics_through() -> None:
    metrics = {"count": 42, "rate": 0.95}
    result = resolve_status(
        "test_agent", [
            (True, "working", "运行中", "none"),
        ], "idle", "等待",
        metrics=metrics,
    )
    assert result["metrics"]["count"] == 42
    assert result["metrics"]["rate"] == 0.95


def test_passes_kwargs_to_build_agent() -> None:
    result = resolve_status(
        "test_agent", [
            (True, "working", "运行中", "none"),
        ], "idle", "等待",
        symbol="XAUUSD",
    )
    assert result.get("symbol") == "XAUUSD"
