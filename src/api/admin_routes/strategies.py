from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query

from src.api import deps
from src.api.admin_schemas import StrategyDetail, StrategyPerformanceReport
from src.api.schemas import ApiResponse
from src.signals.evaluation.calibrator import ConfidenceCalibrator
from src.signals.evaluation.performance import StrategyPerformanceTracker
from src.signals.orchestration import SignalRuntime
from src.signals.service import SignalModule
from src.signals import service_diagnostics as svc_diag
from src.signals.contracts.execution_plan import build_strategy_capability_summary

from .common import build_strategy_detail
from .view_models import ConfidencePipelineView, StrategySessionDetailView

router = APIRouter(prefix="/admin", tags=["admin"])


def _sorted_texts(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    result.sort()
    return result


def _build_module_capability_execution_plan(
    signal_svc: SignalModule,
) -> dict[str, Any]:
    contract = list(signal_svc.strategy_capability_contract())
    strategies = sorted(
        str(item.get("name")).strip()
        for item in contract
        if str(item.get("name") or "").strip()
    )
    summary = build_strategy_capability_summary(
        capability_contract=contract,
        configured_strategies=strategies,
        scheduled_strategies=strategies,
        strategy_timeframes_policy={},
    )
    return {
        "strategies": list(summary.get("scheduled_strategies") or []),
        **summary,
    }


def _extract_backtest_execution_plan(result_payload: Any) -> dict[str, Any] | None:
    if not isinstance(result_payload, dict):
        return None
    raw = result_payload.get("strategy_capability_execution_plan")
    return dict(raw) if isinstance(raw, dict) else None


def _resolve_backtest_execution_plan(run_id: str | None) -> dict[str, Any]:
    from src.backtesting.data import backtest_runtime_store
    from src.api.backtest_routes.execution import get_backtest_repo

    candidate_run_ids: list[str] = []
    normalized_run_id = str(run_id or "").strip()
    if normalized_run_id:
        candidate_run_ids = [normalized_run_id]
    else:
        jobs = sorted(
            backtest_runtime_store.list_jobs(),
            key=lambda row: row.submitted_at,
            reverse=True,
        )
        candidate_run_ids = [str(job.run_id) for job in jobs[:20]]

    repo = get_backtest_repo()

    for candidate in candidate_run_ids:
        cached = backtest_runtime_store.get_result(candidate)
        cached_plan = _extract_backtest_execution_plan(cached)
        if cached_plan is not None:
            return {
                "run_id": candidate,
                "source": "runtime_store",
                "available": True,
                "execution_plan": cached_plan,
            }

        if repo is None:
            continue
        try:
            db_row = repo.fetch_run(candidate)
        except Exception:
            db_row = None
        db_plan = _extract_backtest_execution_plan(db_row)
        if db_plan is not None:
            return {
                "run_id": candidate,
                "source": "repository",
                "available": True,
                "execution_plan": db_plan,
            }

    return {
        "run_id": normalized_run_id or None,
        "source": None,
        "available": False,
        "execution_plan": None,
    }


def _reconcile_execution_plans(
    *,
    module_plan: dict[str, Any],
    runtime_plan: dict[str, Any],
    backtest_plan: dict[str, Any] | None,
) -> dict[str, Any]:
    module_strategies = set(
        _sorted_texts(
            module_plan.get("scheduled_strategies") or module_plan.get("strategies")
        )
    )
    runtime_strategies = set(_sorted_texts(runtime_plan.get("scheduled_strategies")))
    backtest_strategies = (
        set(
            _sorted_texts(
                backtest_plan.get("scheduled_strategies")
                or backtest_plan.get("active_strategies")
            )
        )
        if isinstance(backtest_plan, dict)
        else set()
    )

    module_indicators = set(
        _sorted_texts(module_plan.get("required_indicators_union"))
    )
    runtime_indicators = set(
        _sorted_texts(runtime_plan.get("required_indicators_union"))
    )
    backtest_indicators = (
        set(_sorted_texts(backtest_plan.get("required_indicators_union")))
        if isinstance(backtest_plan, dict)
        else set()
    )

    payload: dict[str, Any] = {
        "module_vs_runtime": {
            "module_only_strategies": sorted(module_strategies - runtime_strategies),
            "runtime_only_strategies": sorted(runtime_strategies - module_strategies),
            "module_only_indicators": sorted(module_indicators - runtime_indicators),
            "runtime_only_indicators": sorted(runtime_indicators - module_indicators),
            "aligned": (
                module_strategies == runtime_strategies
                and module_indicators == runtime_indicators
            ),
        },
        "runtime_vs_backtest": None,
        "module_vs_backtest": None,
    }

    if backtest_plan is not None:
        payload["runtime_vs_backtest"] = {
            "runtime_only_strategies": sorted(runtime_strategies - backtest_strategies),
            "backtest_only_strategies": sorted(backtest_strategies - runtime_strategies),
            "runtime_only_indicators": sorted(runtime_indicators - backtest_indicators),
            "backtest_only_indicators": sorted(backtest_indicators - runtime_indicators),
            "aligned": (
                runtime_strategies == backtest_strategies
                and runtime_indicators == backtest_indicators
            ),
        }
        payload["module_vs_backtest"] = {
            "module_only_strategies": sorted(module_strategies - backtest_strategies),
            "backtest_only_strategies": sorted(backtest_strategies - module_strategies),
            "module_only_indicators": sorted(module_indicators - backtest_indicators),
            "backtest_only_indicators": sorted(backtest_indicators - module_indicators),
            "aligned": (
                module_strategies == backtest_strategies
                and module_indicators == backtest_indicators
            ),
        }
    return payload


@router.get("/performance/strategies", response_model=ApiResponse[StrategyPerformanceReport])
def admin_performance_strategies(
    hours: int = Query(default=168, ge=1, le=720, description="历史统计范围，单位小时。"),
    perf_tracker: StrategyPerformanceTracker = Depends(deps.get_performance_tracker),
    calibrator: ConfidenceCalibrator = Depends(deps.get_calibrator),
    signal_svc: SignalModule = Depends(deps.get_signal_service),
) -> ApiResponse[StrategyPerformanceReport]:
    try:
        ranking = perf_tracker.strategy_ranking()
    except Exception:
        ranking = []
    try:
        summary = perf_tracker.describe()
    except Exception:
        summary = {}
    try:
        winrates = signal_svc.strategy_winrates(hours=hours)
    except Exception:
        winrates = []
    try:
        calibrator_info = calibrator.describe()
    except Exception:
        calibrator_info = {}
    report = StrategyPerformanceReport(session_ranking=ranking, session_summary=summary, historical_winrates=winrates, calibrator=calibrator_info)
    return ApiResponse.success_response(report)


@router.get("/performance/confidence-pipeline/{symbol}/{timeframe}", response_model=ApiResponse[ConfidencePipelineView])
def admin_confidence_pipeline(
    symbol: str,
    timeframe: str,
    signal_runtime: SignalRuntime = Depends(deps.get_signal_runtime),
    signal_svc: SignalModule = Depends(deps.get_signal_service),
    perf_tracker: StrategyPerformanceTracker = Depends(deps.get_performance_tracker),
    calibrator: ConfidenceCalibrator = Depends(deps.get_calibrator),
) -> ApiResponse[ConfidencePipelineView]:
    regime_info = signal_runtime.get_regime_stability(symbol, timeframe) or {}
    strategies_pipeline: List[Dict[str, Any]] = []
    for descriptor in signal_svc.strategy_catalog():
        name = str(descriptor["name"])
        try:
            stats = perf_tracker.get_strategy_stats(name)
        except Exception:
            stats = None
        try:
            multiplier = perf_tracker.get_multiplier(name)
        except Exception:
            multiplier = 1.0
        strategies_pipeline.append({**descriptor, "session_multiplier": multiplier, "session_stats": stats})
    return ApiResponse.success_response(
        ConfidencePipelineView(
            symbol=symbol,
            timeframe=timeframe,
            regime=regime_info,
            calibrator=calibrator.describe(),
            strategies=strategies_pipeline,
        )
    )


@router.get("/strategies", response_model=ApiResponse[List[StrategyDetail]])
def admin_strategies(signal_svc: SignalModule = Depends(deps.get_signal_service)) -> ApiResponse[List[StrategyDetail]]:
    return ApiResponse.success_response([StrategyDetail(**row) for row in signal_svc.strategy_catalog()])


@router.get("/strategies/capability-reconciliation", response_model=ApiResponse[Dict[str, Any]])
def admin_strategy_capability_reconciliation(
    signal_svc: SignalModule = Depends(deps.get_signal_service),
    signal_runtime: SignalRuntime = Depends(deps.get_signal_runtime),
) -> ApiResponse[Dict[str, Any]]:
    payload = svc_diag.strategy_capability_reconciliation(
        signal_svc,
        runtime_policy=signal_runtime.policy,
    )
    return ApiResponse.success_response(payload)


@router.get("/strategies/capability-contract", response_model=ApiResponse[Dict[str, Any]])
def admin_strategy_capability_contract(
    signal_svc: SignalModule = Depends(deps.get_signal_service),
    signal_runtime: SignalRuntime = Depends(deps.get_signal_runtime),
) -> ApiResponse[Dict[str, Any]]:
    return ApiResponse.success_response(
        {
            "module_contract": list(signal_svc.strategy_capability_contract()),
            "runtime_contract": list(signal_runtime.strategy_capability_contract()),
            "runtime_reconciliation": svc_diag.strategy_capability_reconciliation(
                signal_svc,
                runtime_policy=signal_runtime.policy,
            ),
        }
    )


@router.get("/strategies/capability-execution-plan", response_model=ApiResponse[Dict[str, Any]])
def admin_strategy_capability_execution_plan(
    run_id: str | None = Query(
        default=None,
        description="可选：指定 backtest run_id 以对账 runtime 与 backtest 执行计划。",
    ),
    signal_svc: SignalModule = Depends(deps.get_signal_service),
    signal_runtime: SignalRuntime = Depends(deps.get_signal_runtime),
) -> ApiResponse[Dict[str, Any]]:
    module_plan = _build_module_capability_execution_plan(signal_svc)
    runtime_status = signal_runtime.status()
    runtime_plan = dict(runtime_status.get("strategy_capability_execution_plan", {}) or {})

    backtest_snapshot = _resolve_backtest_execution_plan(run_id)
    backtest_plan = (
        dict(backtest_snapshot["execution_plan"])
        if isinstance(backtest_snapshot.get("execution_plan"), dict)
        else None
    )

    return ApiResponse.success_response(
        {
            "module_plan": module_plan,
            "runtime_plan": runtime_plan,
            "backtest_context": {
                "run_id": backtest_snapshot.get("run_id"),
                "source": backtest_snapshot.get("source"),
                "available": bool(backtest_snapshot.get("available")),
            },
            "backtest_plan": backtest_plan,
            "contract_reconciliation": svc_diag.strategy_capability_reconciliation(
                signal_svc,
                runtime_policy=signal_runtime.policy,
            ),
            "execution_plan_reconciliation": _reconcile_execution_plans(
                module_plan=module_plan,
                runtime_plan=runtime_plan,
                backtest_plan=backtest_plan,
            ),
        }
    )


@router.get("/strategies/{name}", response_model=ApiResponse[StrategySessionDetailView])
def admin_strategy_detail(
    name: str,
    signal_svc: SignalModule = Depends(deps.get_signal_service),
    perf_tracker: StrategyPerformanceTracker = Depends(deps.get_performance_tracker),
) -> ApiResponse[StrategySessionDetailView]:
    if name not in signal_svc.list_strategies():
        return ApiResponse.error_response(error_code="VALIDATION_ERROR", error_message=f"Strategy '{name}' not found", suggested_action="Check /v1/admin/strategies for available strategies")
    detail = build_strategy_detail(name, signal_svc)
    try:
        stats = perf_tracker.get_strategy_stats(name)
    except Exception:
        stats = None
    return ApiResponse.success_response(
        StrategySessionDetailView(**detail.model_dump(), session_performance=stats)
    )
