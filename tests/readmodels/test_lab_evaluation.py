"""Tests for LabEvaluationReadModel — P11 Phase 5。

覆盖：
- run_id 不存在（metrics_summary=None）→ 整个 envelope 返 None
- 所有子块 ready → capability 全 "ready"
- 部分子块 missing → capability 精确标记
- 子 ReadModel 抛异常 → 不崩溃，该块标 missing
- freshness 取所有块中最老的 data_updated_at
"""

from __future__ import annotations

from typing import Any, Optional

from src.readmodels.lab_evaluation import LabEvaluationReadModel

# ────────────────────────── Fake sub ReadModels ──────────────────────────


class _FakeBacktestDetail:
    def __init__(
        self,
        *,
        metrics: Optional[dict] = None,
        equity: Optional[dict] = None,
        monthly: Optional[dict] = None,
        trade_structure: Optional[dict] = None,
        execution_realism: Optional[dict] = None,
        raise_block: Optional[str] = None,
    ) -> None:
        self._metrics = metrics
        self._equity = equity
        self._monthly = monthly
        self._ts = trade_structure
        self._er = execution_realism
        self._raise = raise_block

    def build_metrics_summary(self, run_id: str) -> Optional[dict]:
        if self._raise == "metrics_summary":
            raise RuntimeError("metrics boom")
        return self._metrics

    def build_equity_curve(self, run_id: str) -> Optional[dict]:
        if self._raise == "equity_curve":
            raise RuntimeError("equity boom")
        return self._equity

    def build_monthly_returns(self, run_id: str) -> Optional[dict]:
        if self._raise == "monthly_returns":
            raise RuntimeError("monthly boom")
        return self._monthly

    def build_trade_structure(self, run_id: str) -> Optional[dict]:
        if self._raise == "trade_structure":
            raise RuntimeError("trade_struct boom")
        return self._ts

    def build_execution_realism(self, run_id: str) -> Optional[dict]:
        if self._raise == "execution_realism":
            raise RuntimeError("realism boom")
        return self._er


class _FakeWF:
    def __init__(
        self,
        *,
        by_wf: Optional[dict] = None,
        by_backtest: Optional[dict] = None,
    ) -> None:
        self._by_wf = by_wf
        self._by_backtest = by_backtest

    def build_by_wf_run_id(self, run_id: str) -> Optional[dict]:
        return self._by_wf

    def build_by_backtest_run_id(self, run_id: str) -> Optional[dict]:
        return self._by_backtest


class _FakeCorrelation:
    def __init__(self, *, latest: Optional[dict] = None) -> None:
        self._latest = latest

    def build_latest_for_run(self, run_id: str) -> Optional[dict]:
        return self._latest


# ────────────────────────── Fixtures ──────────────────────────


def _freshness(ts: str = "2026-04-21T00:00:00+00:00") -> dict:
    return {
        "observed_at": "2026-04-21T01:00:00+00:00",
        "data_updated_at": ts,
        "age_seconds": 3600.0,
        "stale_after_seconds": 86400.0,
        "max_age_seconds": 2592000.0,
        "freshness_state": "fresh",
        "source_kind": "native",
        "fallback_applied": False,
        "fallback_reason": None,
    }


def _metrics_payload() -> dict:
    return {
        "run_id": "bt_test",
        "total_pnl": 300.0,
        "total_pnl_pct": 0.03,
        "total_trades": 4,
        "winning_trades": 3,
        "losing_trades": 1,
        "win_rate": 0.75,
        "profit_factor": 3.0,
        "expectancy": 75.0,
        "expectancy_r": None,
        "avg_r_multiple": None,
        "max_drawdown": 100.0,
        "max_drawdown_pct": 0.01,
        "max_drawdown_duration_bars": 5,
        "sharpe_ratio": 2.1,
        "sortino_ratio": 2.5,
        "calmar_ratio": 3.2,
        "avg_win": 100.0,
        "avg_loss": -50.0,
        "avg_bars_held": 10.0,
        "max_consecutive_wins": 3,
        "max_consecutive_losses": 1,
        "monthly_returns": [],
        "freshness": _freshness(),
    }


# ────────────────────────── Tests ──────────────────────────


def test_missing_run_returns_none() -> None:
    rm = LabEvaluationReadModel(
        backtest_detail=_FakeBacktestDetail(metrics=None),
        walk_forward=_FakeWF(),
        correlation=_FakeCorrelation(),
    )

    assert rm.build("bt_missing") is None


def test_all_blocks_ready() -> None:
    rm = LabEvaluationReadModel(
        backtest_detail=_FakeBacktestDetail(
            metrics=_metrics_payload(),
            equity={
                "run_id": "bt_test",
                "points": [{"x": 1}],
                "freshness": _freshness(),
            },
            monthly={
                "run_id": "bt_test",
                "months": [{"label": "2026-04"}],
                "freshness": _freshness(),
            },
            trade_structure={
                "run_id": "bt_test",
                "total_trades": 4,
                "partial_data": False,
                "freshness": _freshness(),
            },
            execution_realism={
                "run_id": "bt_test",
                "available": True,
                "freshness": _freshness(),
            },
        ),
        walk_forward=_FakeWF(by_wf={"wf_run_id": "wf_abc", "freshness": _freshness()}),
        correlation=_FakeCorrelation(
            latest={"analysis_id": "corr_1", "freshness": _freshness()}
        ),
    )

    payload = rm.build("bt_test")

    assert payload is not None
    assert payload["run_id"] == "bt_test"
    cap = payload["capability"]
    assert cap["metrics_summary"] == "ready"
    assert cap["equity_curve"] == "ready"
    assert cap["monthly_returns"] == "ready"
    assert cap["trade_structure"] == "ready"
    assert cap["execution_realism"] == "ready"
    assert cap["walk_forward"] == "ready"
    assert cap["correlation_analysis"] == "ready"


def test_missing_blocks_marked_in_capability() -> None:
    """WF/correlation/execution_realism 为 None 时应标 missing。"""
    rm = LabEvaluationReadModel(
        backtest_detail=_FakeBacktestDetail(
            metrics=_metrics_payload(),
            equity={"run_id": "bt_test", "points": [], "freshness": _freshness()},
            monthly={"run_id": "bt_test", "months": [], "freshness": _freshness()},
            trade_structure={
                "run_id": "bt_test",
                "total_trades": 0,
                "partial_data": False,
                "freshness": _freshness(),
            },
            execution_realism={
                "run_id": "bt_test",
                "available": False,
                "freshness": _freshness(),
            },
        ),
        walk_forward=_FakeWF(),
        correlation=_FakeCorrelation(),
    )

    payload = rm.build("bt_test")

    assert payload is not None
    cap = payload["capability"]
    # 空列表 → partial
    assert cap["equity_curve"] == "partial"
    assert cap["monthly_returns"] == "partial"
    # total_trades=0 → partial
    assert cap["trade_structure"] == "partial"
    # available=False → missing
    assert cap["execution_realism"] == "missing"
    # 子 rm 返 None → missing
    assert cap["walk_forward"] == "missing"
    assert cap["correlation_analysis"] == "missing"


def test_subblock_exception_marked_missing_not_crash() -> None:
    """equity 抛异常时，envelope 其他块仍正常，equity 标 missing。"""
    rm = LabEvaluationReadModel(
        backtest_detail=_FakeBacktestDetail(
            metrics=_metrics_payload(),
            raise_block="equity_curve",
        ),
        walk_forward=_FakeWF(),
        correlation=_FakeCorrelation(),
    )

    payload = rm.build("bt_test")

    assert payload is not None
    assert payload["equity_curve"] is None
    assert payload["capability"]["equity_curve"] == "missing"
    # metrics_summary 仍正常
    assert payload["metrics_summary"] is not None


def test_wf_falls_back_to_backtest_run_id() -> None:
    rm = LabEvaluationReadModel(
        backtest_detail=_FakeBacktestDetail(metrics=_metrics_payload()),
        walk_forward=_FakeWF(
            by_wf=None,
            by_backtest={"wf_run_id": "wf_linked", "freshness": _freshness()},
        ),
        correlation=_FakeCorrelation(),
    )

    payload = rm.build("bt_test")

    assert payload is not None
    assert payload["walk_forward"] is not None
    assert payload["walk_forward"]["wf_run_id"] == "wf_linked"
    assert payload["capability"]["walk_forward"] == "ready"


def test_freshness_picks_oldest_data_updated_at() -> None:
    older = _freshness(ts="2026-04-10T00:00:00+00:00")
    newer = _freshness(ts="2026-04-20T00:00:00+00:00")
    metrics = _metrics_payload()
    metrics["freshness"] = newer

    rm = LabEvaluationReadModel(
        backtest_detail=_FakeBacktestDetail(
            metrics=metrics,
            equity={"run_id": "bt_test", "points": [], "freshness": older},
        ),
        walk_forward=_FakeWF(),
        correlation=_FakeCorrelation(),
    )

    payload = rm.build("bt_test")

    assert payload is not None
    # envelope 级 freshness 应选 older（2026-04-10）
    assert payload["freshness"]["data_updated_at"] == "2026-04-10T00:00:00+00:00"
