"""Tests for CorrelationAnalysisReadModel — P11 Phase 3。"""

from __future__ import annotations

from typing import Any

from src.readmodels.correlation_read import CorrelationAnalysisReadModel


class _FakeRepo:
    def __init__(
        self,
        *,
        by_id: dict[str, dict[str, Any]] | None = None,
        latest: dict[str, dict[str, Any]] | None = None,
        list_rows: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._by_id = by_id or {}
        self._latest = latest or {}
        self._list = list_rows or {}

    def fetch(self, analysis_id: str) -> dict[str, Any] | None:
        return self._by_id.get(analysis_id)

    def fetch_latest_for_run(self, backtest_run_id: str) -> dict[str, Any] | None:
        return self._latest.get(backtest_run_id)

    def list_for_run(
        self,
        backtest_run_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return list(self._list.get(backtest_run_id, []))


def _sample_row(
    analysis_id: str = "corr_abc",
    run_id: str = "bt_999",
) -> dict[str, Any]:
    return {
        "analysis_id": analysis_id,
        "backtest_run_id": run_id,
        "created_at": "2026-04-21T00:00:00+00:00",
        "correlation_threshold": 0.80,
        "penalty_weight": 0.50,
        "total_bars_analyzed": 500,
        "strategies_analyzed": 3,
        "high_correlation_count": 1,
        "pairs": [
            {
                "strategy_a": "A",
                "strategy_b": "B",
                "correlation": 0.85,
                "overlap_count": 100,
                "agreement_rate": 0.9,
            }
        ],
        "high_correlation_pairs": [
            {
                "strategy_a": "A",
                "strategy_b": "B",
                "correlation": 0.85,
                "overlap_count": 100,
                "agreement_rate": 0.9,
            }
        ],
        "strategy_weights": {"A": 1.0, "B": 0.5, "C": 1.0},
        "summary": {"all_pairs_count": 3},
    }


def test_build_by_analysis_id_missing_returns_none() -> None:
    repo = _FakeRepo()
    rm = CorrelationAnalysisReadModel(correlation_repo=repo)

    assert rm.build_by_analysis_id("corr_missing") is None


def test_build_by_analysis_id_assembles_full_payload() -> None:
    repo = _FakeRepo(by_id={"corr_abc": _sample_row()})
    rm = CorrelationAnalysisReadModel(correlation_repo=repo)

    payload = rm.build_by_analysis_id("corr_abc")

    assert payload is not None
    assert payload["analysis_id"] == "corr_abc"
    assert payload["backtest_run_id"] == "bt_999"
    assert payload["correlation_threshold"] == 0.80
    assert payload["strategies_analyzed"] == 3
    assert payload["high_correlation_count"] == 1
    assert len(payload["pairs"]) == 1
    assert payload["strategy_weights"] == {"A": 1.0, "B": 0.5, "C": 1.0}

    fresh = payload["freshness"]
    assert fresh["source_kind"] == "native"
    assert "observed_at" in fresh


def test_build_latest_for_run_delegates_correctly() -> None:
    repo = _FakeRepo(latest={"bt_999": _sample_row()})
    rm = CorrelationAnalysisReadModel(correlation_repo=repo)

    payload = rm.build_latest_for_run("bt_999")

    assert payload is not None
    assert payload["backtest_run_id"] == "bt_999"


def test_list_for_run_returns_enriched_items() -> None:
    repo = _FakeRepo(
        list_rows={
            "bt_999": [
                _sample_row(analysis_id="corr_1"),
                _sample_row(analysis_id="corr_2"),
            ]
        }
    )
    rm = CorrelationAnalysisReadModel(correlation_repo=repo)

    items = rm.list_for_run("bt_999")

    assert len(items) == 2
    assert items[0]["analysis_id"] == "corr_1"
    assert items[1]["analysis_id"] == "corr_2"
    # 每项都有 freshness
    assert all("freshness" in it for it in items)


def test_none_repo_returns_empty_or_none() -> None:
    rm = CorrelationAnalysisReadModel(correlation_repo=None)

    assert rm.build_by_analysis_id("corr_abc") is None
    assert rm.build_latest_for_run("bt_999") is None
    assert rm.list_for_run("bt_999") == []
