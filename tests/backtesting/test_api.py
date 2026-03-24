"""Tests for backtest API legacy /results endpoint."""

from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import datetime, timezone
from typing import Any, Dict

import pytest

from src.backtesting.models import BacktestJob, BacktestJobStatus


def _make_job(
    run_id: str,
    job_type: str = "backtest",
    status: BacktestJobStatus = BacktestJobStatus.COMPLETED,
) -> BacktestJob:
    now = datetime.now(timezone.utc)
    return BacktestJob(
        run_id=run_id,
        job_type=job_type,
        status=status,
        submitted_at=now,
        completed_at=now if status == BacktestJobStatus.COMPLETED else None,
        config_summary={"symbol": "XAUUSD", "timeframe": "M5"},
    )


def _sample_result() -> dict:
    return {
        "run_id": "bt_1",
        "metrics": {
            "total_trades": 42,
            "win_rate": 0.55,
            "sharpe_ratio": 1.2,
            "max_drawdown": 0.08,
            "total_pnl": 1500.0,
            "profit_factor": 1.6,
            "sortino_ratio": 1.8,
        },
    }


@pytest.fixture()
def backtest_api():
    """Import backtest_api after src.api is loaded to avoid circular import."""
    import src.api  # noqa: F401  — ensure __init__ runs first
    import src.backtesting.api as mod

    return mod


def _run(coro):  # type: ignore[no-untyped-def]
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_list_results_returns_result_summaries(backtest_api) -> None:
    """Legacy /results should return type + metrics, not raw job dicts."""
    orig_jobs = dict(backtest_api._job_store)
    orig_cache = dict(backtest_api._result_cache)
    try:
        backtest_api._job_store.clear()
        backtest_api._result_cache.clear()

        job = _make_job("bt_1", job_type="backtest")
        backtest_api._job_store["bt_1"] = job
        backtest_api._result_cache["bt_1"] = _sample_result()

        response = _run(backtest_api.list_results())

        assert response.success is True
        results = response.data
        assert len(results) == 1
        entry = results[0]
        # Legacy contract fields
        assert entry["type"] == "backtest"
        assert entry["run_id"] == "bt_1"
        assert "metrics" in entry
        assert entry["metrics"]["total_trades"] == 42
        assert entry["metrics"]["sharpe_ratio"] == 1.2
        # Should NOT contain raw job-only fields
        assert "job_type" not in entry
        assert "progress" not in entry
    finally:
        backtest_api._job_store.clear()
        backtest_api._job_store.update(orig_jobs)
        backtest_api._result_cache.clear()
        backtest_api._result_cache.update(orig_cache)


def test_list_results_optimization_includes_count(backtest_api) -> None:
    orig_jobs = dict(backtest_api._job_store)
    orig_cache = dict(backtest_api._result_cache)
    try:
        backtest_api._job_store.clear()
        backtest_api._result_cache.clear()

        job = _make_job("opt_1", job_type="optimization")
        backtest_api._job_store["opt_1"] = job
        backtest_api._result_cache["opt_1"] = [
            _sample_result(),
            {**_sample_result(), "run_id": "opt_1_2"},
        ]

        response = _run(backtest_api.list_results())

        assert response.success is True
        entry = response.data[0]
        assert entry["type"] == "optimization"
        assert entry["metrics"]["optimization_count"] == 2
        assert entry["metrics"]["best"]["total_trades"] == 42
    finally:
        backtest_api._job_store.clear()
        backtest_api._job_store.update(orig_jobs)
        backtest_api._result_cache.clear()
        backtest_api._result_cache.update(orig_cache)


def test_list_results_pending_job_has_no_metrics(backtest_api) -> None:
    orig_jobs = dict(backtest_api._job_store)
    orig_cache = dict(backtest_api._result_cache)
    try:
        backtest_api._job_store.clear()
        backtest_api._result_cache.clear()

        job = _make_job("bt_2", status=BacktestJobStatus.PENDING)
        backtest_api._job_store["bt_2"] = job

        response = _run(backtest_api.list_results())

        assert response.success is True
        entry = response.data[0]
        assert entry["status"] == "pending"
        assert "metrics" not in entry
    finally:
        backtest_api._job_store.clear()
        backtest_api._job_store.update(orig_jobs)
        backtest_api._result_cache.clear()
        backtest_api._result_cache.update(orig_cache)
