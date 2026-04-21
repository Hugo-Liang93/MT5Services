from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.api import deps
from src.backtesting.data import backtest_runtime_store

from . import schemas as config_service

logger = logging.getLogger(__name__)


def cleanup_components(components: Optional[Dict[str, Any]]) -> None:
    if components is None:
        return
    pipeline = components.get("pipeline")
    if pipeline is not None:
        try:
            pipeline.shutdown()
        except Exception:
            logger.debug("Pipeline shutdown error", exc_info=True)
    writer = components.get("writer")
    if writer is not None:
        try:
            writer.close()
        except Exception:
            logger.debug("Writer close error", exc_info=True)


def persist_result(result: Any) -> None:
    try:
        repo = get_backtest_repo()
        if repo is not None:
            repo.save_result(result)
    except Exception:
        logger.warning(
            "Failed to persist backtest result %s", result.run_id, exc_info=True
        )


def _update_experiment_on_backtest(result: Any) -> None:
    """侧效：回测完成后更新 experiments 表（experiment_id 为空时跳过）。"""
    exp_id = getattr(result, "experiment_id", None)
    if not exp_id:
        return
    try:
        exp_repo = deps.get_experiment_repo()
        if exp_repo is None:
            return
        exp_repo.advance_to_backtest(exp_id, result.run_id)
        metrics = getattr(result, "metrics", None)
        if metrics is not None:
            exp_repo.record_backtest_metrics(
                exp_id,
                sharpe=getattr(metrics, "sharpe_ratio", 0.0),
                win_rate=getattr(metrics, "win_rate", 0.0),
            )
    except Exception:
        logger.debug("Failed to update experiment %s", exp_id, exc_info=True)


def get_backtest_repo() -> Optional[Any]:
    """获取共享的 BacktestRepository（来自 container.storage_writer）。

    禁止在此构造独立 TimescaleWriter——历史教训见 `deps.get_backtest_repo` 注释。
    """
    try:
        return deps.get_backtest_repo()
    except Exception:
        logger.debug("BacktestRepository not available", exc_info=True)
        return None


def get_walk_forward_repo() -> Optional[Any]:
    """获取共享的 WalkForwardRepository（来自 container.storage_writer）。

    与 BacktestRepository 同一连接池，ADR-006 禁止独立构造 writer。
    """
    try:
        return deps.get_walk_forward_repo()
    except Exception:
        logger.debug("WalkForwardRepository not available", exc_info=True)
        return None


def get_correlation_repo() -> Optional[Any]:
    """获取共享的 CorrelationAnalysisRepository（P11 Phase 3）。"""
    try:
        return deps.get_correlation_repo()
    except Exception:
        logger.debug("CorrelationAnalysisRepository not available", exc_info=True)
        return None


def _persist_walk_forward_result(
    *,
    wf_run_id: str,
    wf_result: Any,
    experiment_id: Optional[str],
    started_at: Any,
    completed_at: Any,
) -> None:
    """落库 WF 结果到 backtest_walk_forward_runs/_windows。

    失败不阻塞 WF 完成（内存 store 已有 hot cache），仅记 warning。
    """
    repo = get_walk_forward_repo()
    if repo is None:
        return
    try:
        repo.ensure_schema()
        repo.save(
            wf_result,
            wf_run_id=wf_run_id,
            backtest_run_id=None,
            started_at=started_at,
            completed_at=completed_at,
            experiment_id=experiment_id,
        )
    except Exception:
        logger.warning(
            "Failed to persist walk-forward result %s", wf_run_id, exc_info=True
        )


def _persist_walk_forward_failed(
    *,
    wf_run_id: str,
    error: str,
    experiment_id: Optional[str],
) -> None:
    """WF 失败时记录失败快照，方便事后回查。"""
    repo = get_walk_forward_repo()
    if repo is None:
        return
    try:
        repo.ensure_schema()
        repo.save_failed(
            wf_run_id=wf_run_id,
            error=error,
            backtest_run_id=None,
            experiment_id=experiment_id,
        )
    except Exception:
        logger.debug(
            "Failed to persist failed walk-forward %s", wf_run_id, exc_info=True
        )


def build_api_components(
    strategy_params: Optional[Dict[str, Any]] = None,
    strategy_params_per_tf: Optional[Dict[str, Dict[str, Any]]] = None,
    regime_affinity_overrides: Optional[Dict[str, Dict[str, float]]] = None,
) -> Dict[str, Any]:
    from src.backtesting.component_factory import build_backtest_components

    return build_backtest_components(
        strategy_params=strategy_params,
        strategy_params_per_tf=strategy_params_per_tf,
        regime_affinity_overrides=regime_affinity_overrides,
    )


def extract_result_metrics(cached: Any, job_type: str) -> Dict[str, Any]:
    if job_type == "optimization" and isinstance(cached, list):
        return {
            "optimization_count": len(cached),
            "best": pick_metrics(cached[0]) if cached else {},
        }
    if isinstance(cached, dict):
        return pick_metrics(cached)
    return {}


def pick_metrics(result: Dict[str, Any]) -> Dict[str, Any]:
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        return {}
    return {
        k: metrics[k]
        for k in (
            "total_trades",
            "win_rate",
            "sharpe_ratio",
            "max_drawdown",
            "total_pnl",
            "profit_factor",
        )
        if k in metrics
    }


def execute_backtest(run_id: str, request: config_service.BacktestRunRequest) -> None:
    acquired = backtest_runtime_store.semaphore.acquire(timeout=5)
    if not acquired:
        backtest_runtime_store.fail_job(
            run_id, "另一个回测/优化任务正在执行，请稍后重试"
        )
        return

    backtest_runtime_store.start_job(run_id)
    components: Optional[Dict[str, Any]] = None
    try:
        from src.backtesting.engine import BacktestEngine

        config = config_service.build_backtest_config(request)
        components = build_api_components(
            strategy_params=request.strategy_params or None,
            strategy_params_per_tf=request.strategy_params_per_tf or None,
            regime_affinity_overrides=request.regime_affinity_overrides or None,
        )
        engine = BacktestEngine(
            config=config,
            data_loader=components["data_loader"],
            signal_module=components["signal_module"],
            indicator_pipeline=components["pipeline"],
            regime_detector=components["regime_detector"],
        )
        result = engine.run()
        result.experiment_id = request.experiment_id
        persist_result(result)
        _update_experiment_on_backtest(result)
        backtest_runtime_store.complete_job(run_id, result.to_dict())
    except Exception as exc:
        logger.exception("Backtest %s failed", run_id)
        backtest_runtime_store.fail_job(run_id, str(exc))
    finally:
        cleanup_components(components)
        backtest_runtime_store.semaphore.release()


def execute_optimization(
    run_id: str, request: config_service.BacktestOptimizeRequest
) -> None:
    acquired = backtest_runtime_store.semaphore.acquire(timeout=5)
    if not acquired:
        backtest_runtime_store.fail_job(
            run_id, "另一个回测/优化任务正在执行，请稍后重试"
        )
        return

    backtest_runtime_store.start_job(run_id)
    components: Optional[Dict[str, Any]] = None
    try:
        from src.backtesting.models import ParameterSpace
        from src.backtesting.optimization import (
            ParameterOptimizer,
            build_signal_module_with_overrides,
        )

        optimizer_settings = config_service.resolve_optimizer_settings(request)
        config = config_service.build_backtest_config(request)
        param_space = ParameterSpace(
            strategy_params=request.param_space,
            search_mode=optimizer_settings["search_mode"],
            max_combinations=optimizer_settings["max_combinations"],
        )
        components = build_api_components(
            strategy_params=request.strategy_params or None,
            strategy_params_per_tf=request.strategy_params_per_tf or None,
            regime_affinity_overrides=request.regime_affinity_overrides or None,
        )
        base_module = components["signal_module"]

        def module_factory(params: Dict[str, Any]) -> Any:
            return build_signal_module_with_overrides(base_module, params)

        optimizer = ParameterOptimizer(
            base_config=config,
            param_space=param_space,
            data_loader=components["data_loader"],
            indicator_pipeline=components["pipeline"],
            signal_module_factory=module_factory,
            regime_detector=components["regime_detector"],
            sort_metric=optimizer_settings["sort_metric"],
        )
        results = optimizer.run()
        for result in results:
            result.experiment_id = request.experiment_id
            persist_result(result)
        backtest_runtime_store.complete_job(
            run_id, [result.to_dict() for result in results[:50]]
        )
    except Exception as exc:
        logger.exception("Optimization %s failed", run_id)
        backtest_runtime_store.fail_job(run_id, str(exc))
    finally:
        cleanup_components(components)
        backtest_runtime_store.semaphore.release()


def execute_walk_forward(
    run_id: str, request: config_service.WalkForwardRequest
) -> None:
    from datetime import datetime, timezone

    acquired = backtest_runtime_store.semaphore.acquire(timeout=5)
    if not acquired:
        backtest_runtime_store.fail_job(
            run_id, "另一个回测/优化任务正在执行，请稍后重试"
        )
        return

    backtest_runtime_store.start_job(run_id)
    components: Optional[Dict[str, Any]] = None
    started_at = datetime.now(timezone.utc)
    try:
        from src.backtesting.models import ParameterSpace
        from src.backtesting.optimization import (
            WalkForwardConfig,
            WalkForwardValidator,
            build_signal_module_with_overrides,
        )

        optimizer_settings = config_service.resolve_optimizer_settings(request)
        base_config = config_service.build_backtest_config(request)
        param_space = ParameterSpace(
            strategy_params=request.param_space,
            search_mode=optimizer_settings["search_mode"],
            max_combinations=optimizer_settings["max_combinations"],
        )
        wf_config = WalkForwardConfig(
            total_start_time=base_config.start_time,
            total_end_time=base_config.end_time,
            base_config=base_config,
            train_ratio=request.train_ratio,
            n_splits=request.n_splits,
            anchored=request.anchored,
            optimization_metric=optimizer_settings["sort_metric"],
            param_space=param_space,
        )
        components = build_api_components(
            strategy_params=request.strategy_params or None,
            strategy_params_per_tf=request.strategy_params_per_tf or None,
            regime_affinity_overrides=request.regime_affinity_overrides or None,
        )
        base_module = components["signal_module"]

        def module_factory(params: Dict[str, Any]) -> Any:
            return build_signal_module_with_overrides(base_module, params)

        validator = WalkForwardValidator(
            config=wf_config,
            data_loader=components["data_loader"],
            signal_module_factory=module_factory,
            indicator_pipeline=components["pipeline"],
            regime_detector=components["regime_detector"],
        )
        wf_result = validator.run()
        completed_at = datetime.now(timezone.utc)
        # 先落库（失败不阻塞），再写内存 hot cache
        _persist_walk_forward_result(
            wf_run_id=run_id,
            wf_result=wf_result,
            experiment_id=request.experiment_id,
            started_at=started_at,
            completed_at=completed_at,
        )
        backtest_runtime_store.store_walk_forward_result(run_id, wf_result)
        for split in wf_result.splits:
            split.out_of_sample_result.experiment_id = request.experiment_id
            persist_result(split.out_of_sample_result)
        summary = {
            "run_id": run_id,
            "n_splits": len(wf_result.splits),
            "overfitting_ratio": round(wf_result.overfitting_ratio, 4),
            "consistency_rate": round(wf_result.consistency_rate, 4),
            "aggregate_metrics": {
                "total_trades": wf_result.aggregate_metrics.total_trades,
                "win_rate": wf_result.aggregate_metrics.win_rate,
                "sharpe_ratio": wf_result.aggregate_metrics.sharpe_ratio,
                "max_drawdown": wf_result.aggregate_metrics.max_drawdown,
                "total_pnl": wf_result.aggregate_metrics.total_pnl,
                "profit_factor": wf_result.aggregate_metrics.profit_factor,
            },
            "splits": [
                {
                    "split_index": split.split_index,
                    "best_params": split.best_params,
                    "in_sample_sharpe": split.in_sample_result.metrics.sharpe_ratio,
                    "out_of_sample_sharpe": split.out_of_sample_result.metrics.sharpe_ratio,
                }
                for split in wf_result.splits
            ],
        }
        backtest_runtime_store.complete_job(run_id, summary)
    except Exception as exc:
        logger.exception("Walk-Forward %s failed", run_id)
        _persist_walk_forward_failed(
            wf_run_id=run_id,
            error=str(exc),
            experiment_id=request.experiment_id,
        )
        backtest_runtime_store.fail_job(run_id, str(exc))
    finally:
        cleanup_components(components)
        backtest_runtime_store.semaphore.release()
