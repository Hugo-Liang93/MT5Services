"""Tests for GET /v1/backtest/history/{run_id}/correlation-analysis — P11 Phase 3。"""

from __future__ import annotations

from typing import Any

from src.api.backtest_routes.detail.correlation import get_correlation_analysis
from src.readmodels.correlation_read import CorrelationAnalysisReadModel


class _FakeRepo:
    def __init__(
        self,
        *,
        by_id: dict[str, dict[str, Any]] | None = None,
        latest: dict[str, dict[str, Any]] | None = None,
        raise_on_fetch: bool = False,
    ) -> None:
        self._by_id = by_id or {}
        self._latest = latest or {}
        self._raise = raise_on_fetch

    def fetch(self, analysis_id: str) -> dict[str, Any] | None:
        if self._raise:
            raise RuntimeError("db down")
        return self._by_id.get(analysis_id)

    def fetch_latest_for_run(self, backtest_run_id: str) -> dict[str, Any] | None:
        if self._raise:
            raise RuntimeError("db down")
        return self._latest.get(backtest_run_id)

    def list_for_run(
        self,
        backtest_run_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list:
        return []


def _row(analysis_id: str = "corr_abc", run_id: str = "bt_999") -> dict[str, Any]:
    return {
        "analysis_id": analysis_id,
        "backtest_run_id": run_id,
        "created_at": "2026-04-21T00:00:00+00:00",
        "correlation_threshold": 0.80,
        "penalty_weight": 0.50,
        "total_bars_analyzed": 500,
        "strategies_analyzed": 3,
        "high_correlation_count": 0,
        "pairs": [],
        "high_correlation_pairs": [],
        "strategy_weights": {"A": 1.0},
        "summary": {},
    }


def test_route_latest_for_run_returns_detail() -> None:
    repo = _FakeRepo(latest={"bt_999": _row()})
    rm = CorrelationAnalysisReadModel(correlation_repo=repo)

    response = get_correlation_analysis(
        run_id="bt_999", analysis_id=None, read_model=rm
    )

    assert response.success is True
    assert response.data is not None
    assert response.data.analysis_id == "corr_abc"
    assert response.data.backtest_run_id == "bt_999"


def test_route_by_analysis_id_returns_detail() -> None:
    repo = _FakeRepo(by_id={"corr_custom": _row(analysis_id="corr_custom")})
    rm = CorrelationAnalysisReadModel(correlation_repo=repo)

    response = get_correlation_analysis(
        run_id="bt_999", analysis_id="corr_custom", read_model=rm
    )

    assert response.success is True
    assert response.data is not None
    assert response.data.analysis_id == "corr_custom"


def test_route_not_found_envelope() -> None:
    repo = _FakeRepo()
    rm = CorrelationAnalysisReadModel(correlation_repo=repo)

    response = get_correlation_analysis(
        run_id="bt_missing", analysis_id=None, read_model=rm
    )

    assert response.success is False
    assert response.data is None
    assert response.error is not None
    assert response.error["code"] == "not_found"
    assert response.error["details"]["run_id"] == "bt_missing"


def test_route_internal_error_on_repo_exception() -> None:
    repo = _FakeRepo(raise_on_fetch=True)
    rm = CorrelationAnalysisReadModel(correlation_repo=repo)

    response = get_correlation_analysis(
        run_id="bt_999", analysis_id=None, read_model=rm
    )

    assert response.success is False
    assert response.error is not None
    assert response.error["code"] == "internal_server_error"
