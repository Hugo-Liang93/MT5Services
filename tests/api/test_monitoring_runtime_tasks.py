from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from src.api import deps
from src.api.monitoring import get_runtime_tasks


def test_get_runtime_task_status_formats_database_rows(monkeypatch) -> None:
    row = (
        "startup",
        "monitoring",
        datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        "ready",
        datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
        123,
        1,
        0,
        0,
        None,
        {"startup": True},
    )

    monkeypatch.setattr(deps, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(
        deps,
        "_container",
        SimpleNamespace(
            storage_writer=SimpleNamespace(
                db=SimpleNamespace(
                    fetch_runtime_task_status=lambda component=None, task_name=None: [row]
                )
            )
        ),
    )

    items = deps.get_runtime_task_status(component="startup", task_name="monitoring")

    assert items == [
        {
            "component": "startup",
            "task_name": "monitoring",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "state": "ready",
            "started_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:01:00+00:00",
            "next_run_at": "2026-01-01T01:00:00+00:00",
            "duration_ms": 123,
            "success_count": 1,
            "failure_count": 0,
            "consecutive_failures": 0,
            "last_error": None,
            "details": {"startup": True},
        }
    ]


def test_runtime_tasks_endpoint_returns_items_and_filters(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.api.monitoring.get_runtime_task_status",
        lambda component=None, task_name=None: [
            {
                "component": component,
                "task_name": task_name,
                "state": "ready",
                "details": {"startup": True},
            }
        ],
    )

    response = asyncio.run(get_runtime_tasks(component="startup", task_name="monitoring"))

    assert response["items"] == [
        {
            "component": "startup",
            "task_name": "monitoring",
            "state": "ready",
            "details": {"startup": True},
        }
    ]
    assert response["filters"] == {"component": "startup", "task_name": "monitoring"}
