"""Tests for GET /v1/backtest/history/{run_id}/walk-forward — P11 Phase 2。

直接调路由函数（跳过 FastAPI DI），传 ReadModel 作参数。覆盖：
- wf_run_id 命中
- wf_run_id 不命中但作为 backtest_run_id 命中
- 都不命中 → 404 envelope
- ReadModel raises → internal error envelope
"""

from __future__ import annotations

from typing import Any

from src.api.backtest_routes.detail.walk_forward import get_walk_forward_detail
from src.readmodels.walk_forward_read import WalkForwardReadModel


class _FakeRepo:
    def __init__(
        self,
        *,
        by_wf: dict[str, dict[str, Any]] | None = None,
        by_backtest: dict[str, dict[str, Any]] | None = None,
        raise_on_fetch: bool = False,
    ) -> None:
        self._by_wf = by_wf or {}
        self._by_backtest = by_backtest or {}
        self._raise = raise_on_fetch

    def fetch(self, wf_run_id: str) -> dict[str, Any] | None:
        if self._raise:
            raise RuntimeError("db down")
        return self._by_wf.get(wf_run_id)

    def fetch_latest_by_backtest_run(
        self, backtest_run_id: str
    ) -> dict[str, Any] | None:
        if self._raise:
            raise RuntimeError("db down")
        return self._by_backtest.get(backtest_run_id)


def _bundle(wf_run_id: str = "wf_abc") -> dict[str, Any]:
    return {
        "run": {
            "wf_run_id": wf_run_id,
            "backtest_run_id": None,
            "created_at": "2026-04-20T00:00:00+00:00",
            "completed_at": "2026-04-20T00:10:00+00:00",
            "config": {"symbol": "XAUUSD"},
            "aggregate_metrics": {},
            "overfitting_ratio": 1.1,
            "consistency_rate": 0.8,
            "oos_sharpe": 1.2,
            "oos_win_rate": 0.6,
            "oos_total_trades": 20,
            "oos_total_pnl": 200.0,
            "n_splits": 2,
            "status": "completed",
            "duration_ms": 30000,
            "error": None,
            "experiment_id": None,
        },
        "windows": [],
    }


def test_route_returns_detail_when_wf_run_id_matches() -> None:
    repo = _FakeRepo(by_wf={"wf_abc": _bundle()})
    rm = WalkForwardReadModel(walk_forward_repo=repo)

    response = get_walk_forward_detail(run_id="wf_abc", read_model=rm)

    assert response.success is True
    assert response.data is not None
    assert response.data.wf_run_id == "wf_abc"
    assert response.data.aggregate.overfitting_ratio == 1.1


def test_route_falls_back_to_backtest_run_id_lookup() -> None:
    repo = _FakeRepo(by_backtest={"bt_999": _bundle(wf_run_id="wf_xyz")})
    rm = WalkForwardReadModel(walk_forward_repo=repo)

    response = get_walk_forward_detail(run_id="bt_999", read_model=rm)

    assert response.success is True
    assert response.data is not None
    assert response.data.wf_run_id == "wf_xyz"


def test_route_returns_not_found_envelope_when_nothing_matches() -> None:
    repo = _FakeRepo()
    rm = WalkForwardReadModel(walk_forward_repo=repo)

    response = get_walk_forward_detail(run_id="missing", read_model=rm)

    assert response.success is False
    assert response.data is None
    assert response.error is not None
    assert response.error["code"] == "not_found"
    assert response.error["details"] == {"run_id": "missing"}


def test_route_returns_internal_error_on_repo_exception() -> None:
    repo = _FakeRepo(raise_on_fetch=True)
    rm = WalkForwardReadModel(walk_forward_repo=repo)

    response = get_walk_forward_detail(run_id="wf_abc", read_model=rm)

    assert response.success is False
    assert response.error is not None
    assert response.error["code"] == "internal_server_error"
