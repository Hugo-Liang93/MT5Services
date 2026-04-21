"""Tests for WalkForwardReadModel — P11 Phase 2。

覆盖：
- wf_run_id 不存在 → None
- wf_run_id 命中 → 装配 aggregate / windows / freshness
- backtest_run_id fallback 查找
- repo 为 None → 降级 None
"""

from __future__ import annotations

from typing import Any

from src.readmodels.walk_forward_read import WalkForwardReadModel


class _FakeRepo:
    def __init__(
        self,
        *,
        by_wf: dict[str, dict[str, Any]] | None = None,
        by_backtest: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._by_wf = by_wf or {}
        self._by_backtest = by_backtest or {}

    def fetch(self, wf_run_id: str) -> dict[str, Any] | None:
        return self._by_wf.get(wf_run_id)

    def fetch_latest_by_backtest_run(
        self, backtest_run_id: str
    ) -> dict[str, Any] | None:
        return self._by_backtest.get(backtest_run_id)


def _sample_bundle() -> dict[str, Any]:
    return {
        "run": {
            "wf_run_id": "wf_abc",
            "backtest_run_id": None,
            "created_at": "2026-04-20T00:00:00+00:00",
            "completed_at": "2026-04-20T00:10:00+00:00",
            "config": {"symbol": "XAUUSD", "n_splits": 3},
            "aggregate_metrics": {"sharpe_ratio": 1.2},
            "overfitting_ratio": 1.5,
            "consistency_rate": 0.67,
            "oos_sharpe": 1.0,
            "oos_win_rate": 0.55,
            "oos_total_trades": 30,
            "oos_total_pnl": 300.0,
            "n_splits": 3,
            "status": "completed",
            "duration_ms": 60000,
            "error": None,
            "experiment_id": "exp_1",
        },
        "windows": [
            {
                "split_index": 0,
                "window_label": "2026-01-01→2026-02-01",
                "train_start": "2026-01-01T00:00:00+00:00",
                "train_end": "2026-01-20T00:00:00+00:00",
                "test_start": "2026-01-20T00:00:00+00:00",
                "test_end": "2026-02-01T00:00:00+00:00",
                "best_params": {"p": 0},
                "is_metrics": {"sharpe_ratio": 1.5},
                "oos_metrics": {"sharpe_ratio": 1.0},
                "is_pnl": 50.0,
                "oos_pnl": 30.0,
                "is_sharpe": 1.5,
                "oos_sharpe": 1.0,
                "is_win_rate": 0.6,
                "oos_win_rate": 0.5,
                "oos_max_drawdown": 20.0,
                "oos_trade_count": 10,
            },
        ],
    }


# ────────────────────────── build_by_wf_run_id ──────────────────────────


def test_build_by_wf_run_id_missing_returns_none() -> None:
    repo = _FakeRepo()
    rm = WalkForwardReadModel(walk_forward_repo=repo)

    assert rm.build_by_wf_run_id("wf_missing") is None


def test_build_by_wf_run_id_assembles_full_payload() -> None:
    repo = _FakeRepo(by_wf={"wf_abc": _sample_bundle()})
    rm = WalkForwardReadModel(walk_forward_repo=repo)

    payload = rm.build_by_wf_run_id("wf_abc")

    assert payload is not None
    assert payload["wf_run_id"] == "wf_abc"
    assert payload["status"] == "completed"
    assert payload["n_splits"] == 3

    # aggregate 字段完整
    aggregate = payload["aggregate"]
    assert aggregate["overfitting_ratio"] == 1.5
    assert aggregate["consistency_rate"] == 0.67
    assert aggregate["oos_sharpe"] == 1.0
    assert aggregate["oos_total_trades"] == 30

    # windows 完整
    assert len(payload["windows"]) == 1
    win = payload["windows"][0]
    assert win["split_index"] == 0
    assert win["oos_sharpe"] == 1.0
    assert win["oos_trade_count"] == 10
    assert win["best_params"] == {"p": 0}

    # freshness 契约
    fresh = payload["freshness"]
    assert fresh["source_kind"] == "native"
    assert "observed_at" in fresh
    assert "freshness_state" in fresh


# ────────────────────────── build_by_backtest_run_id ──────────────────────────


def test_build_by_backtest_run_id_uses_latest_per_backtest() -> None:
    repo = _FakeRepo(by_backtest={"bt_123": _sample_bundle()})
    rm = WalkForwardReadModel(walk_forward_repo=repo)

    payload = rm.build_by_backtest_run_id("bt_123")
    assert payload is not None
    assert payload["wf_run_id"] == "wf_abc"


def test_build_by_backtest_run_id_missing_returns_none() -> None:
    repo = _FakeRepo()
    rm = WalkForwardReadModel(walk_forward_repo=repo)

    assert rm.build_by_backtest_run_id("bt_missing") is None


# ────────────────────────── Repo 不可用 ──────────────────────────


def test_none_repo_returns_none() -> None:
    rm = WalkForwardReadModel(walk_forward_repo=None)

    assert rm.build_by_wf_run_id("wf_abc") is None
    assert rm.build_by_backtest_run_id("bt_123") is None
