"""Tests for backtest API routes and configuration helpers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

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
    import src.api  # noqa: F401
    from src.api.backtest_routes import schemas as api_config
    from src.api.backtest_routes import config as config_routes
    from src.api.backtest_routes import jobs as jobs_routes
    from src.backtesting.data import store as runtime_store

    return SimpleNamespace(
        api_config=api_config,
        config_routes=config_routes,
        jobs_routes=jobs_routes,
        store=runtime_store.backtest_runtime_store,
    )


def _run(coro):  # type: ignore[no-untyped-def]
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _snapshot_state(store) -> tuple[dict[str, BacktestJob], dict[str, object]]:
    jobs = {job.run_id: job for job in store.list_jobs()}
    results = {run_id: store.get_result(run_id) for run_id in jobs}
    return jobs, results


def _restore_state(store, jobs: dict[str, BacktestJob], results: dict[str, object]) -> None:
    store.reset()
    for job in jobs.values():
        store.register_job(job)
    for run_id, result in results.items():
        if result is not None:
            store.set_result(run_id, result)


def test_list_results_returns_result_summaries(backtest_api) -> None:
    store = backtest_api.store
    orig_jobs, orig_results = _snapshot_state(store)
    try:
        store.reset()
        store.register_job(_make_job("bt_1", job_type="backtest"))
        store.set_result("bt_1", _sample_result())

        response = _run(backtest_api.jobs_routes.list_results())

        assert response.success is True
        results = response.data
        assert len(results) == 1
        entry = results[0]
        assert entry["type"] == "backtest"
        assert entry["run_id"] == "bt_1"
        assert "metrics" in entry
        assert entry["metrics"]["total_trades"] == 42
        assert entry["metrics"]["sharpe_ratio"] == 1.2
        assert "job_type" not in entry
        assert "progress" not in entry
    finally:
        _restore_state(store, orig_jobs, orig_results)


def test_list_results_optimization_includes_count(backtest_api) -> None:
    store = backtest_api.store
    orig_jobs, orig_results = _snapshot_state(store)
    try:
        store.reset()
        store.register_job(_make_job("opt_1", job_type="optimization"))
        store.set_result(
            "opt_1",
            [
                _sample_result(),
                {**_sample_result(), "run_id": "opt_1_2"},
            ],
        )

        response = _run(backtest_api.jobs_routes.list_results())

        assert response.success is True
        entry = response.data[0]
        assert entry["type"] == "optimization"
        assert entry["metrics"]["optimization_count"] == 2
        assert entry["metrics"]["best"]["total_trades"] == 42
    finally:
        _restore_state(store, orig_jobs, orig_results)


def test_list_results_pending_job_has_no_metrics(backtest_api) -> None:
    store = backtest_api.store
    orig_jobs, orig_results = _snapshot_state(store)
    try:
        store.reset()
        store.register_job(_make_job("bt_2", status=BacktestJobStatus.PENDING))

        response = _run(backtest_api.jobs_routes.list_results())

        assert response.success is True
        entry = response.data[0]
        assert entry["status"] == "pending"
        assert "metrics" not in entry
    finally:
        _restore_state(store, orig_jobs, orig_results)


def test_build_backtest_config_uses_defaults_and_overrides(
    backtest_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backtest_api.api_config,
        "load_backtest_defaults",
        lambda: {
            "initial_balance": 15000.0,
            "max_positions": 4,
            "risk_percent": 2.5,
            "min_volume": 0.02,
            "max_volume": 0.8,
            "max_volume_per_order": 0.15,
            "max_volume_per_symbol": 0.30,
            "daily_loss_limit_pct": 8.0,
            "max_trades_per_day": 6,
            "max_trades_per_hour": 2,
            "filter_allowed_sessions": "asia,london,new_york",
            "enable_state_machine": True,
        },
    )
    monkeypatch.setattr(
        backtest_api.api_config,
        "load_signal_config",
        lambda: SimpleNamespace(
            strategy_timeframes={"ema_cross": ["M5", "M15"]},
            strategy_sessions={"ema_cross": ["london", "new_york"]},
        ),
    )
    request = backtest_api.api_config.BacktestRunRequest(
        symbol="XAUUSD",
        timeframe="M5",
        start_time="2025-01-01",
        end_time="2025-01-31",
        risk_percent=1.2,
        max_volume_per_day=0.5,
        strategy_params={"ema_cross_fast": 12},
        strategy_params_per_tf={"M5": {"ema_cross_fast": 9}},
        regime_affinity_overrides={"ema_cross": {"trend": 1.3}},
    )

    config = backtest_api.api_config.build_backtest_config(request)

    assert config.initial_balance == 15000.0
    assert config.risk.max_positions == 4
    assert config.position.risk_percent == 1.2
    assert config.position.min_volume == 0.02
    assert config.position.max_volume == 0.8
    assert config.risk.max_volume_per_order == 0.15
    assert config.risk.max_volume_per_symbol == 0.30
    assert config.risk.max_volume_per_day == 0.5
    assert config.risk.daily_loss_limit_pct == 8.0
    assert config.risk.max_trades_per_day == 6
    assert config.risk.max_trades_per_hour == 2
    assert config.filters.allowed_sessions == "asia,london,new_york"
    assert config.enable_state_machine is True
    assert config.strategy_params == {"ema_cross_fast": 12}
    assert config.strategy_params_per_tf == {"M5": {"ema_cross_fast": 9}}
    assert config.regime_affinity_overrides == {"ema_cross": {"trend": 1.3}}
    assert config.strategy_timeframes == {"ema_cross": ["M5", "M15"]}
    assert config.strategy_sessions == {"ema_cross": ["london", "new_york"]}


def test_run_optimization_job_summary_uses_default_optimizer_settings(
    backtest_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backtest_api.api_config,
        "load_backtest_defaults",
        lambda: {
            "search_mode": "random",
            "max_combinations": 77,
            "sort_metric": "profit_factor",
        },
    )
    store = backtest_api.store
    orig_jobs, orig_results = _snapshot_state(store)
    try:
        store.reset()
        request = backtest_api.api_config.BacktestOptimizeRequest(
            symbol="XAUUSD",
            timeframe="M5",
            start_time="2025-01-01",
            end_time="2025-01-31",
            param_space={"ema_cross_fast": [9, 12]},
        )

        response = _run(
            backtest_api.jobs_routes.run_optimization(request, BackgroundTasks())
        )

        assert response.success is True
        summary = response.data["config_summary"]
        assert summary["search_mode"] == "random"
        assert summary["max_combinations"] == 77
    finally:
        _restore_state(store, orig_jobs, orig_results)


def test_get_backtest_config_defaults_exposes_supported_fields(
    backtest_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backtest_api.api_config,
        "load_backtest_defaults",
        lambda: {"risk_percent": 2.0, "search_mode": "grid"},
    )

    response = _run(backtest_api.config_routes.get_backtest_config_defaults())

    assert response.success is True
    assert response.data["defaults"]["risk_percent"] == 2.0
    supported = response.data["supported"]
    assert "regime_affinity_overrides" in supported["run_fields"]
    assert "sort_metric" in supported["optimize_fields"]
    assert "anchored" in supported["walk_forward_fields"]
    assert "random" in supported["search_modes"]


def test_get_param_space_template_uses_effective_timeframe_params(
    backtest_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backtest_api.api_config,
        "load_signal_config",
        lambda: SimpleNamespace(
            strategy_timeframes={
                "rsi_reversion": ["M5", "M15"],
                "supertrend": ["M5", "M15"],
            },
            strategy_params={
                "rsi_reversion__overbought": 78.0,
                "rsi_reversion__oversold": 22.0,
                "supertrend__adx_threshold": 21.0,
            },
            strategy_params_per_tf={
                "M5": {
                    "rsi_reversion__overbought": 72.0,
                    "rsi_reversion__oversold": 25.0,
                }
            },
            regime_affinity_overrides={},
        ),
    )

    response = _run(
        backtest_api.config_routes.get_backtest_param_space_template(
            timeframe="M5",
            strategies="rsi_reversion,supertrend",
        )
    )

    assert response.success is True
    data = response.data
    assert data["resolved_strategies"] == ["rsi_reversion", "supertrend"]
    assert data["baseline_strategy_params"]["rsi_reversion__overbought"] == 72.0
    assert data["baseline_strategy_params"]["rsi_reversion__oversold"] == 25.0
    assert data["baseline_strategy_params"]["supertrend__adx_threshold"] == 21.0
    assert data["param_space"]["rsi_reversion__overbought"] == [66.0, 69.0, 72.0, 75.0, 78.0]
    assert data["param_space"]["supertrend__adx_threshold"] == [18.0, 19.0, 21.0, 23.0, 25.0]


def test_get_param_space_template_auto_filters_by_timeframe(
    backtest_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backtest_api.api_config,
        "load_signal_config",
        lambda: SimpleNamespace(
            strategy_timeframes={
                "rsi_reversion": ["M5", "M15"],
                "session_momentum": ["M15", "M30"],
                "multi_timeframe_confirm": ["M30", "H1"],
            },
            strategy_params={
                "rsi_reversion__overbought": 78.0,
                "session_momentum__london_min_atr_pct": 0.00050,
                "session_momentum__other_min_atr_pct": 0.00038,
            },
            strategy_params_per_tf={},
            regime_affinity_overrides={},
        ),
    )

    response = _run(
        backtest_api.config_routes.get_backtest_param_space_template(
            timeframe="M30",
            strategies=None,
        )
    )

    assert response.success is True
    data = response.data
    assert data["resolved_strategies"] == ["session_momentum", "multi_timeframe_confirm"]
    assert "session_momentum__london_min_atr_pct" in data["param_space"]
    assert "rsi_reversion__overbought" not in data["param_space"]
