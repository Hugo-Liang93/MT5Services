"""LabImpactReadModel 测试（ADR-010 后）。

paper_sessions 字段已替换为 demo_validation_windows（按策略+时间窗聚合 demo trade_outcomes）。
linked_paper_sessions 字段移除。
"""

from __future__ import annotations

from threading import Lock
from typing import Any

from src.readmodels.lab_impact import LabImpactReadModel


class _FakeBacktestStore:
    def __init__(self) -> None:
        self.walk_forward_results: dict[str, Any] = {}
        self.recommendations: dict[str, Any] = {}
        self.walk_forward_lock = Lock()
        self.recommendation_lock = Lock()


class _FakeTradeOutcomeRepo:
    """提供 fetch_demo_validation_windows 接口的最小 stub。"""

    def __init__(self, windows: list[dict[str, Any]] | None = None) -> None:
        self._windows = windows or []

    def fetch_demo_validation_windows(
        self, *, window_start: Any, window_end: Any, limit: int = 50
    ) -> list[dict[str, Any]]:
        return list(self._windows[:limit])


def test_lab_impact_returns_canonical_4_block_payload() -> None:
    store = _FakeBacktestStore()
    store.walk_forward_results["run_1"] = {
        "status": "completed",
        "source_run_id": "run_1",
        "overfitting_ratio": 0.2,
        "consistency_rate": 0.85,
        "oos_sharpe": 1.8,
        "phases": [
            {
                "window_id": "w1",
                "train_period": {"start": "2025-01-01", "end": "2025-06-30"},
                "validation_period": {"start": "2025-07-01", "end": "2025-09-30"},
                "is_sharpe": 2.1,
                "oos_sharpe": 1.8,
            }
        ],
        "experiment_id": "exp_A",
    }
    store.recommendations["rec_1"] = {
        "rec_id": "rec_1",
        "source_run_id": "run_1",
        "status": "approved",
        "rationale": "high OOS Sharpe",
        "experiment_id": "exp_A",
    }
    trade_outcome_repo = _FakeTradeOutcomeRepo(
        windows=[
            {
                "strategy": "structured_trend_continuation",
                "timeframe": "H1",
                "window_start": "2026-04-18T00:00:00+00:00",
                "window_end": "2026-04-25T00:00:00+00:00",
                "total_trades": 8,
                "win_rate": 0.625,
                "total_pnl": 145.30,
            }
        ],
    )
    model = LabImpactReadModel(
        backtest_store=store, trade_outcome_repo=trade_outcome_repo
    )

    payload = model.build_impact()

    wf = payload["walk_forward_snapshots"]
    assert len(wf) == 1
    assert wf[0]["run_id"] == "run_1"
    assert wf[0]["phases"][0]["oos_sharpe"] == 1.8

    recs = payload["recommendations"]
    assert len(recs) == 1
    assert recs[0]["rec_id"] == "rec_1"

    windows = payload["demo_validation_windows"]
    assert len(windows) == 1
    assert windows[0]["strategy"] == "structured_trend_continuation"
    assert windows[0]["total_trades"] == 8

    links = {e["experiment_id"]: e for e in payload["experiment_links"]}
    assert "exp_A" in links
    assert "run_1" in links["exp_A"]["backtest_run_ids"]
    assert "rec_1" in links["exp_A"]["recommendation_ids"]

    assert payload["freshness"]["source_kind"] == "native"


def test_lab_impact_handles_empty_stores() -> None:
    model = LabImpactReadModel(
        backtest_store=_FakeBacktestStore(),
        trade_outcome_repo=_FakeTradeOutcomeRepo(),
    )

    payload = model.build_impact()

    assert payload["walk_forward_snapshots"] == []
    assert payload["recommendations"] == []
    assert payload["demo_validation_windows"] == []
    assert payload["experiment_links"] == []


def test_lab_impact_handles_missing_trade_outcome_repo() -> None:
    """trade_outcome_repo=None 时返回空数组，不应崩溃。"""
    model = LabImpactReadModel(
        backtest_store=_FakeBacktestStore(),
        trade_outcome_repo=None,
    )
    payload = model.build_impact()
    assert payload["demo_validation_windows"] == []


class _FakeBacktestRepo:
    def __init__(self, recommendations: list[Any]) -> None:
        self._recs = recommendations

    def fetch_recommendations(self, limit: int = 50) -> list[Any]:
        return list(self._recs[:limit])


def test_lab_impact_reads_from_db_when_store_empty() -> None:
    """P0.1 修复：内存 store 空时，DB 回读 recommendations。"""
    store = _FakeBacktestStore()
    db_rec = {
        "rec_id": "rec_db_1",
        "source_run_id": "run_db_1",
        "status": "applied",
        "created_at": "2026-04-19T10:00:00+00:00",
        "experiment_id": "exp_C",
    }
    model = LabImpactReadModel(
        backtest_store=store,
        trade_outcome_repo=_FakeTradeOutcomeRepo(),
        backtest_repo=_FakeBacktestRepo([db_rec]),
    )

    payload = model.build_impact()

    recs = payload["recommendations"]
    assert len(recs) == 1
    assert recs[0]["rec_id"] == "rec_db_1"
    assert recs[0]["status"] == "applied"


def test_lab_impact_memory_store_overrides_db_on_same_rec_id() -> None:
    """P0.1 修复：内存 store 和 DB 都有同 rec_id 时，内存优先（更新鲜）。"""
    store = _FakeBacktestStore()
    store.recommendations["rec_1"] = {
        "rec_id": "rec_1",
        "status": "pending",
        "created_at": "2026-04-20T10:00:00+00:00",
    }
    db_rec = {
        "rec_id": "rec_1",
        "status": "applied",
        "created_at": "2026-04-20T09:00:00+00:00",
    }
    model = LabImpactReadModel(
        backtest_store=store,
        trade_outcome_repo=_FakeTradeOutcomeRepo(),
        backtest_repo=_FakeBacktestRepo([db_rec]),
    )

    payload = model.build_impact()

    recs = payload["recommendations"]
    assert len(recs) == 1
    assert recs[0]["status"] == "pending"


def test_lab_impact_merges_store_and_db_by_created_at_desc() -> None:
    store = _FakeBacktestStore()
    store.recommendations["rec_A"] = {
        "rec_id": "rec_A",
        "status": "pending",
        "created_at": "2026-04-18T00:00:00+00:00",
    }
    db_recs = [
        {
            "rec_id": "rec_B",
            "status": "approved",
            "created_at": "2026-04-20T00:00:00+00:00",
        },
        {
            "rec_id": "rec_C",
            "status": "applied",
            "created_at": "2026-04-19T00:00:00+00:00",
        },
    ]
    model = LabImpactReadModel(
        backtest_store=store,
        trade_outcome_repo=_FakeTradeOutcomeRepo(),
        backtest_repo=_FakeBacktestRepo(db_recs),
    )

    payload = model.build_impact()
    recs = payload["recommendations"]

    assert [r["rec_id"] for r in recs] == ["rec_B", "rec_C", "rec_A"]


def test_lab_impact_respects_limits() -> None:
    store = _FakeBacktestStore()
    for i in range(5):
        store.walk_forward_results[f"run_{i}"] = {
            "status": "completed",
            "source_run_id": f"run_{i}",
        }
    model = LabImpactReadModel(
        backtest_store=store,
        trade_outcome_repo=_FakeTradeOutcomeRepo(),
    )

    payload = model.build_impact(wf_limit=2)

    assert len(payload["walk_forward_snapshots"]) == 2
