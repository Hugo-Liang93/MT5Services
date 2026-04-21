"""Tests for BacktestDetailReadModel — P11 Phase 1。

覆盖：
- run_id 不存在 → None
- metrics_summary 字段完整 + drawdown_pct 派生
- equity_curve 的 drawdown / drawdown_pct / pnl_cumulative 派生
- monthly_returns 按月聚合 + trade_count 按 exit_time 归属
- R 倍数计算（有有效 stop_loss + 无有效 stop_loss 两种场景）
"""

from __future__ import annotations

from typing import Any

from src.readmodels.backtest_detail import BacktestDetailReadModel


class _FakeBacktestRepo:
    def __init__(
        self,
        *,
        runs: dict[str, dict[str, Any]] | None = None,
        trades: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._runs = runs or {}
        self._trades = trades or {}

    def fetch_run(self, run_id: str) -> dict[str, Any] | None:
        return self._runs.get(run_id)

    def fetch_trades(self, run_id: str) -> list[dict[str, Any]]:
        return list(self._trades.get(run_id, []))


def _minimal_run(
    *,
    run_id: str = "bt_test",
    created_at: str = "2026-04-20T00:00:00+00:00",
    initial_balance: float = 10000.0,
    metrics: dict[str, Any] | None = None,
    equity_curve: list[Any] | None = None,
) -> dict[str, Any]:
    default_metrics = {
        "total_trades": 10,
        "winning_trades": 6,
        "losing_trades": 4,
        "win_rate": 0.6,
        "profit_factor": 2.0,
        "expectancy": 100.0,
        "sharpe_ratio": 1.5,
        "sortino_ratio": 2.0,
        "calmar_ratio": 3.0,
        "max_drawdown": 250.0,
        "max_drawdown_duration": 15,
        "avg_win": 200.0,
        "avg_loss": -100.0,
        "avg_bars_held": 12.5,
        "total_pnl": 800.0,
        "total_pnl_pct": 0.08,
        "max_consecutive_wins": 4,
        "max_consecutive_losses": 2,
    }
    if metrics:
        default_metrics.update(metrics)
    return {
        "run_id": run_id,
        "created_at": created_at,
        "config": {"initial_balance": initial_balance},
        "metrics": default_metrics,
        "equity_curve": equity_curve or [],
        "param_set": {},
        "metrics_by_regime": {},
        "metrics_by_strategy": {},
        "status": "completed",
        "duration_ms": 1000,
        "filter_stats": None,
    }


# ────────────────────────── run_id 不存在 ──────────────────────────


def test_missing_run_returns_none() -> None:
    repo = _FakeBacktestRepo()
    rm = BacktestDetailReadModel(backtest_repo=repo)

    assert rm.build_metrics_summary("not_exist") is None
    assert rm.build_equity_curve("not_exist") is None
    assert rm.build_monthly_returns("not_exist") is None


# ────────────────────────── metrics_summary ──────────────────────────


def test_metrics_summary_exposes_all_fixed_fields() -> None:
    run = _minimal_run(initial_balance=10000.0)
    repo = _FakeBacktestRepo(runs={"bt_test": run})
    rm = BacktestDetailReadModel(backtest_repo=repo)

    payload = rm.build_metrics_summary("bt_test")

    assert payload is not None
    # 所有契约字段存在
    expected_keys = {
        "run_id",
        "total_pnl",
        "total_pnl_pct",
        "total_trades",
        "winning_trades",
        "losing_trades",
        "win_rate",
        "profit_factor",
        "expectancy",
        "expectancy_r",
        "avg_r_multiple",
        "max_drawdown",
        "max_drawdown_pct",
        "max_drawdown_duration_bars",
        "sharpe_ratio",
        "sortino_ratio",
        "calmar_ratio",
        "avg_win",
        "avg_loss",
        "avg_bars_held",
        "max_consecutive_wins",
        "max_consecutive_losses",
        "monthly_returns",
        "freshness",
    }
    assert expected_keys <= set(payload.keys())

    # 派生字段：max_drawdown_pct = 250 / 10000 = 0.025
    assert payload["max_drawdown_pct"] == 0.025

    # trades 缺失 → R 字段为 None
    assert payload["expectancy_r"] is None
    assert payload["avg_r_multiple"] is None


def test_metrics_summary_r_multiple_from_trades() -> None:
    """trades 含有效 stop_loss 时，R 倍数 = pnl / (|entry - sl| × size)。"""
    run = _minimal_run(initial_balance=10000.0)
    trades = [
        # risk = |100 - 99| × 1 = 1；pnl = 2 → R = 2
        {
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "position_size": 1.0,
            "pnl": 2.0,
            "exit_time": "2026-04-05T10:00:00+00:00",
        },
        # risk = |100 - 99| × 1 = 1；pnl = -1 → R = -1
        {
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "position_size": 1.0,
            "pnl": -1.0,
            "exit_time": "2026-04-08T12:00:00+00:00",
        },
        # 无效 stop_loss（= entry） → 跳过
        {
            "entry_price": 100.0,
            "stop_loss": 100.0,
            "position_size": 1.0,
            "pnl": 0.0,
            "exit_time": "2026-04-09T10:00:00+00:00",
        },
    ]
    repo = _FakeBacktestRepo(runs={"bt_test": run}, trades={"bt_test": trades})
    rm = BacktestDetailReadModel(backtest_repo=repo)

    payload = rm.build_metrics_summary("bt_test")

    assert payload is not None
    # (2 + -1) / 2 = 0.5
    assert payload["expectancy_r"] == 0.5
    assert payload["avg_r_multiple"] == 0.5


# ────────────────────────── equity_curve ──────────────────────────


def test_equity_curve_expands_drawdown() -> None:
    """drawdown = running_max - equity；drawdown_pct = drawdown / running_max。"""
    run = _minimal_run(
        initial_balance=10000.0,
        equity_curve=[
            ["2026-04-01T00:00:00+00:00", 10000.0],
            ["2026-04-02T00:00:00+00:00", 10500.0],  # running_max = 10500
            ["2026-04-03T00:00:00+00:00", 10200.0],  # drawdown = 300
            ["2026-04-04T00:00:00+00:00", 10800.0],  # running_max = 10800
            ["2026-04-05T00:00:00+00:00", 10400.0],  # drawdown = 400
        ],
    )
    repo = _FakeBacktestRepo(runs={"bt_test": run})
    rm = BacktestDetailReadModel(backtest_repo=repo)

    payload = rm.build_equity_curve("bt_test")

    assert payload is not None
    assert payload["initial_balance"] == 10000.0
    points = payload["points"]
    assert len(points) == 5

    # 首点：running_max = 10000，drawdown = 0
    assert points[0]["drawdown"] == 0.0
    assert points[0]["drawdown_pct"] == 0.0
    assert points[0]["pnl_cumulative"] == 0.0

    # 第 3 点：running_max = 10500，drawdown = 300，drawdown_pct ≈ 0.02857
    assert points[2]["drawdown"] == 300.0
    assert abs(points[2]["drawdown_pct"] - 300.0 / 10500.0) < 1e-9
    assert points[2]["pnl_cumulative"] == 200.0

    # 第 5 点：running_max = 10800，drawdown = 400
    assert points[4]["drawdown"] == 400.0
    assert abs(points[4]["drawdown_pct"] - 400.0 / 10800.0) < 1e-9
    assert points[4]["pnl_cumulative"] == 400.0


def test_equity_curve_handles_empty() -> None:
    run = _minimal_run(equity_curve=[])
    repo = _FakeBacktestRepo(runs={"bt_test": run})
    rm = BacktestDetailReadModel(backtest_repo=repo)

    payload = rm.build_equity_curve("bt_test")
    assert payload is not None
    assert payload["points"] == []


# ────────────────────────── monthly_returns ──────────────────────────


def test_monthly_returns_groups_by_month() -> None:
    run = _minimal_run(
        initial_balance=10000.0,
        equity_curve=[
            # 2026-03
            ["2026-03-01T00:00:00+00:00", 10000.0],
            ["2026-03-15T00:00:00+00:00", 10100.0],
            ["2026-03-31T00:00:00+00:00", 10200.0],
            # 2026-04
            ["2026-04-01T00:00:00+00:00", 10300.0],
            ["2026-04-30T00:00:00+00:00", 10500.0],
        ],
    )
    trades = [
        {
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "position_size": 1.0,
            "pnl": 50.0,
            "exit_time": "2026-03-10T10:00:00+00:00",
        },
        {
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "position_size": 1.0,
            "pnl": 50.0,
            "exit_time": "2026-03-20T10:00:00+00:00",
        },
        {
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "position_size": 1.0,
            "pnl": 200.0,
            "exit_time": "2026-04-15T10:00:00+00:00",
        },
    ]
    repo = _FakeBacktestRepo(runs={"bt_test": run}, trades={"bt_test": trades})
    rm = BacktestDetailReadModel(backtest_repo=repo)

    payload = rm.build_monthly_returns("bt_test")

    assert payload is not None
    months = payload["months"]
    assert len(months) == 2

    # 首月 2026-03：初始 10000，月末 10200 → pnl = 200，return_pct = 0.02
    assert months[0]["label"] == "2026-03"
    assert months[0]["pnl"] == 200.0
    assert abs(months[0]["return_pct"] - 0.02) < 1e-9
    assert months[0]["trade_count"] == 2

    # 2026-04：starting 10200（上月末），月末 10500 → pnl = 300
    assert months[1]["label"] == "2026-04"
    assert months[1]["pnl"] == 300.0
    assert abs(months[1]["return_pct"] - 300.0 / 10200.0) < 1e-9
    assert months[1]["trade_count"] == 1


def test_monthly_returns_handles_empty_curve() -> None:
    run = _minimal_run(equity_curve=[])
    repo = _FakeBacktestRepo(runs={"bt_test": run})
    rm = BacktestDetailReadModel(backtest_repo=repo)

    payload = rm.build_monthly_returns("bt_test")
    assert payload is not None
    assert payload["months"] == []


# ────────────────────────── freshness ──────────────────────────


def test_freshness_block_contract() -> None:
    """freshness payload 对齐 src.readmodels.freshness.build_freshness_block。"""
    run = _minimal_run()
    repo = _FakeBacktestRepo(runs={"bt_test": run})
    rm = BacktestDetailReadModel(backtest_repo=repo)

    payload = rm.build_metrics_summary("bt_test")

    assert payload is not None
    fresh = payload["freshness"]
    # 核心字段存在
    for key in (
        "observed_at",
        "data_updated_at",
        "age_seconds",
        "stale_after_seconds",
        "max_age_seconds",
        "freshness_state",
        "source_kind",
        "fallback_applied",
    ):
        assert key in fresh
    assert fresh["source_kind"] == "native"
    assert fresh["fallback_applied"] is False


# ────────────────────────── Repo 不可用降级 ──────────────────────────


def test_none_repo_returns_none() -> None:
    rm = BacktestDetailReadModel(backtest_repo=None)
    assert rm.build_metrics_summary("bt_test") is None
    assert rm.build_equity_curve("bt_test") is None
    assert rm.build_monthly_returns("bt_test") is None
    assert rm.build_trade_structure("bt_test") is None
    assert rm.build_execution_realism("bt_test") is None


# ────────────────────────── Phase 4a: trade_structure ──────────────────────────


def _trade(
    *,
    pnl: float = 10.0,
    pnl_pct: float = 0.1,
    hold_minutes: int | None = 30,
    mfe_pct: float | None = 1.2,
    mae_pct: float | None = 0.4,
    entry_price: float = 100.0,
    stop_loss: float = 99.0,
    position_size: float = 1.0,
    exit_time: str = "2026-04-05T10:00:00+00:00",
) -> dict:
    return {
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "hold_minutes": hold_minutes,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "position_size": position_size,
        "exit_time": exit_time,
    }


def test_trade_structure_on_empty_trades() -> None:
    run = _minimal_run()
    repo = _FakeBacktestRepo(runs={"bt_test": run})
    rm = BacktestDetailReadModel(backtest_repo=repo)

    payload = rm.build_trade_structure("bt_test")

    assert payload is not None
    assert payload["total_trades"] == 0
    assert payload["partial_data"] is False
    assert payload["avg_hold_minutes"] is None
    assert payload["max_loss_streak"] == 0
    assert payload["mfe_distribution"] == []


def test_trade_structure_full_fields() -> None:
    run = _minimal_run()
    trades = [
        _trade(pnl=10.0, hold_minutes=30, mfe_pct=1.0, mae_pct=0.5),
        _trade(pnl=-5.0, hold_minutes=45, mfe_pct=0.8, mae_pct=1.2),
        _trade(pnl=-3.0, hold_minutes=120, mfe_pct=0.3, mae_pct=0.8),
        _trade(pnl=8.0, hold_minutes=200, mfe_pct=1.5, mae_pct=0.3),
    ]
    repo = _FakeBacktestRepo(runs={"bt_test": run}, trades={"bt_test": trades})
    rm = BacktestDetailReadModel(backtest_repo=repo)

    payload = rm.build_trade_structure("bt_test")

    assert payload is not None
    assert payload["total_trades"] == 4
    assert payload["partial_data"] is False
    # 持仓：(30+45+120+200)/4 = 98.75
    assert payload["avg_hold_minutes"] == 98.75
    # 中位数 = (45+120)/2 = 82.5
    assert payload["median_hold_minutes"] == 82.5
    # 连亏：第 2-3 笔连续亏损 → 2
    assert payload["max_loss_streak"] == 2
    assert payload["max_win_streak"] == 1
    # avg_mfe = (1.0+0.8+0.3+1.5)/4 = 0.9
    assert payload["avg_mfe_pct"] == 0.9
    # 分布桶存在
    assert len(payload["mfe_distribution"]) == 5
    assert len(payload["mae_distribution"]) == 5
    # holding_buckets 4 桶
    assert len(payload["holding_buckets"]) == 4
    # <1h 桶：30min + 45min = 2 笔
    assert payload["holding_buckets"][0]["label"] == "<1h"
    assert payload["holding_buckets"][0]["trade_count"] == 2


def test_trade_structure_partial_data_when_fields_missing() -> None:
    run = _minimal_run()
    trades = [
        _trade(pnl=5.0, hold_minutes=None, mfe_pct=None, mae_pct=None),
        _trade(pnl=-2.0, hold_minutes=60, mfe_pct=0.5, mae_pct=0.3),
    ]
    repo = _FakeBacktestRepo(runs={"bt_test": run}, trades={"bt_test": trades})
    rm = BacktestDetailReadModel(backtest_repo=repo)

    payload = rm.build_trade_structure("bt_test")

    assert payload is not None
    assert payload["total_trades"] == 2
    assert payload["partial_data"] is True
    # hold_values 只有 1 条
    assert payload["avg_hold_minutes"] == 60.0
    # streaks 基于 pnl 不受缺失影响
    assert payload["max_win_streak"] == 1
    assert payload["max_loss_streak"] == 1


# ────────────────────────── Phase 4a: execution_realism ──────────────────────────


def test_execution_realism_not_available_for_legacy_run() -> None:
    run = _minimal_run()  # execution_realism 字段不存在
    repo = _FakeBacktestRepo(runs={"bt_test": run})
    rm = BacktestDetailReadModel(backtest_repo=repo)

    payload = rm.build_execution_realism("bt_test")

    assert payload is not None
    assert payload["available"] is False
    assert payload["spread_sensitivity"] is None
    assert payload["slippage_sensitivity"] is None
    assert payload["broker_variance"] is None
    assert payload["session_dependence"] is None


def test_execution_realism_available_when_row_has_payload() -> None:
    run = _minimal_run()
    run["execution_realism"] = {
        "spread_sensitivity": 0.12,
        "slippage_sensitivity": 0.05,
        "broker_variance": 0.08,
        "session_dependence": 0.33,
    }
    repo = _FakeBacktestRepo(runs={"bt_test": run})
    rm = BacktestDetailReadModel(backtest_repo=repo)

    payload = rm.build_execution_realism("bt_test")

    assert payload is not None
    assert payload["available"] is True
    assert payload["spread_sensitivity"] == 0.12
    assert payload["session_dependence"] == 0.33
