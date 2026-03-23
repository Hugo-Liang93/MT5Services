"""回测 FastAPI 路由。

路由前缀: /v1/backtest
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, Field

from src.api.schemas import ApiResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/backtest", tags=["backtest"])

# 简易内存结果存储（生产环境可替换为 DB）
_results_store: Dict[str, Dict[str, Any]] = {}
_results_lock = threading.Lock()


# ── Request / Response Schemas ──────────────────────────────────


class BacktestRunRequest(BaseModel):
    symbol: str
    timeframe: str
    start_time: str = Field(..., description="ISO 格式起始时间 (YYYY-MM-DD)")
    end_time: str = Field(..., description="ISO 格式结束时间 (YYYY-MM-DD)")
    strategies: Optional[List[str]] = None
    initial_balance: float = 10000.0
    min_confidence: float = 0.55
    warmup_bars: int = 200
    strategy_params: Dict[str, Any] = Field(default_factory=dict)


class BacktestOptimizeRequest(BaseModel):
    symbol: str
    timeframe: str
    start_time: str
    end_time: str
    strategies: Optional[List[str]] = None
    initial_balance: float = 10000.0
    min_confidence: float = 0.55
    warmup_bars: int = 200
    param_space: Dict[str, List[Any]] = Field(
        ..., description="参数搜索空间 {key: [val1, val2, ...]}"
    )
    search_mode: str = "grid"
    max_combinations: int = 500
    sort_metric: str = "sharpe_ratio"


class BacktestStatusResponse(BaseModel):
    run_id: str
    status: str  # "running" | "completed" | "failed"
    message: Optional[str] = None


# ── 路由实现 ────────────────────────────────────────────────────


@router.post("/run", response_model=ApiResponse)
async def run_backtest(
    request: BacktestRunRequest,
    background_tasks: BackgroundTasks,
) -> ApiResponse:
    """触发单次回测（异步执行）。"""
    run_id = f"bt_{uuid.uuid4().hex[:12]}"

    with _results_lock:
        _results_store[run_id] = {"status": "running", "result": None, "error": None}

    background_tasks.add_task(_execute_backtest, run_id, request)

    return ApiResponse(
        success=True,
        data={"run_id": run_id, "status": "running"},
    )


@router.post("/optimize", response_model=ApiResponse)
async def run_optimization(
    request: BacktestOptimizeRequest,
    background_tasks: BackgroundTasks,
) -> ApiResponse:
    """触发参数优化（异步执行）。"""
    run_id = f"opt_{uuid.uuid4().hex[:12]}"

    with _results_lock:
        _results_store[run_id] = {"status": "running", "result": None, "error": None}

    background_tasks.add_task(_execute_optimization, run_id, request)

    return ApiResponse(
        success=True,
        data={"run_id": run_id, "status": "running"},
    )


@router.get("/results", response_model=ApiResponse)
async def list_results() -> ApiResponse:
    """列出所有回测结果。"""
    with _results_lock:
        summaries = []
        for run_id, entry in _results_store.items():
            summary: Dict[str, Any] = {"run_id": run_id, "status": entry["status"]}
            if entry["result"] is not None:
                if isinstance(entry["result"], list):
                    # 优化结果
                    summary["type"] = "optimization"
                    summary["count"] = len(entry["result"])
                else:
                    summary["type"] = "backtest"
                    summary["metrics"] = _extract_metrics(entry["result"])
            summaries.append(summary)

    return ApiResponse(success=True, data=summaries)


@router.get("/results/{run_id}", response_model=ApiResponse)
async def get_result(run_id: str) -> ApiResponse:
    """获取单次回测详情。"""
    with _results_lock:
        entry = _results_store.get(run_id)

    if entry is None:
        return ApiResponse(success=False, error=f"Run {run_id} not found")

    if entry["status"] == "running":
        return ApiResponse(
            success=True,
            data={"run_id": run_id, "status": "running"},
        )

    if entry["status"] == "failed":
        return ApiResponse(
            success=False,
            error=entry.get("error", "Unknown error"),
        )

    return ApiResponse(success=True, data=entry["result"])


# ── 后台执行函数 ────────────────────────────────────────────────


def _execute_backtest(run_id: str, request: BacktestRunRequest) -> None:
    """在后台线程执行回测。"""
    try:
        from src.backtesting.models import BacktestConfig
        from src.backtesting.engine import BacktestEngine

        config = BacktestConfig(
            symbol=request.symbol,
            timeframe=request.timeframe,
            start_time=datetime.fromisoformat(request.start_time).replace(tzinfo=timezone.utc),
            end_time=datetime.fromisoformat(request.end_time).replace(tzinfo=timezone.utc),
            strategies=request.strategies,
            initial_balance=request.initial_balance,
            min_confidence=request.min_confidence,
            warmup_bars=request.warmup_bars,
            strategy_params=request.strategy_params,
        )

        # 构建组件
        components = _build_api_components()
        engine = BacktestEngine(
            config=config,
            data_loader=components["data_loader"],
            signal_module=components["signal_module"],
            indicator_pipeline=components["pipeline"],
            regime_detector=components["regime_detector"],
        )

        result = engine.run()
        result_dict = result.to_dict()

        with _results_lock:
            _results_store[run_id] = {
                "status": "completed",
                "result": result_dict,
                "error": None,
            }

    except Exception as e:
        logger.exception("Backtest %s failed", run_id)
        with _results_lock:
            _results_store[run_id] = {
                "status": "failed",
                "result": None,
                "error": str(e),
            }


def _execute_optimization(run_id: str, request: BacktestOptimizeRequest) -> None:
    """在后台线程执行参数优化。"""
    try:
        from src.backtesting.models import BacktestConfig, ParameterSpace
        from src.backtesting.optimizer import ParameterOptimizer, build_signal_module_with_overrides

        config = BacktestConfig(
            symbol=request.symbol,
            timeframe=request.timeframe,
            start_time=datetime.fromisoformat(request.start_time).replace(tzinfo=timezone.utc),
            end_time=datetime.fromisoformat(request.end_time).replace(tzinfo=timezone.utc),
            strategies=request.strategies,
            initial_balance=request.initial_balance,
            min_confidence=request.min_confidence,
            warmup_bars=request.warmup_bars,
        )

        param_space = ParameterSpace(
            strategy_params=request.param_space,
            search_mode=request.search_mode,
            max_combinations=request.max_combinations,
        )

        components = _build_api_components()
        base_module = components["signal_module"]

        def module_factory(params: Dict[str, Any]):  # type: ignore[no-untyped-def]
            return build_signal_module_with_overrides(base_module, params)

        optimizer = ParameterOptimizer(
            base_config=config,
            param_space=param_space,
            data_loader=components["data_loader"],
            indicator_pipeline=components["pipeline"],
            signal_module_factory=module_factory,
            regime_detector=components["regime_detector"],
            sort_metric=request.sort_metric,
        )

        results = optimizer.run()
        results_dicts = [r.to_dict() for r in results[:50]]  # 最多返回 50 组

        with _results_lock:
            _results_store[run_id] = {
                "status": "completed",
                "result": results_dicts,
                "error": None,
            }

    except Exception as e:
        logger.exception("Optimization %s failed", run_id)
        with _results_lock:
            _results_store[run_id] = {
                "status": "failed",
                "result": None,
                "error": str(e),
            }


def _build_api_components() -> Dict[str, Any]:
    """构建回测所需组件（API 上下文）。"""
    from src.config.database import get_db_config
    from src.persistence.db import TimescaleWriter
    from src.persistence.repositories.market_repo import MarketRepository
    from src.signals.evaluation.regime import MarketRegimeDetector
    from src.signals.service import SignalModule
    from src.indicators.engine.pipeline import get_global_pipeline
    from src.config.indicator_config import get_global_config_manager

    from .data_loader import HistoricalDataLoader

    db_config = get_db_config()
    writer = TimescaleWriter(
        host=db_config.host,
        port=db_config.port,
        dbname=db_config.dbname,
        user=db_config.user,
        password=db_config.password,
    )
    market_repo = MarketRepository(writer)
    data_loader = HistoricalDataLoader(market_repo)

    config_manager = get_global_config_manager()
    indicator_config = config_manager.get_config()
    pipeline = get_global_pipeline(indicator_config.pipeline)

    import importlib
    for ind_cfg in indicator_config.indicators:
        if not ind_cfg.enabled:
            continue
        parts = ind_cfg.func_path.rsplit(".", 1)
        mod = importlib.import_module(parts[0])
        func = getattr(mod, parts[1])
        pipeline.register_indicator(
            name=ind_cfg.name,
            func=func,
            params=ind_cfg.params,
            dependencies=ind_cfg.dependencies or None,
        )

    class _NullIndicatorSource:
        def get_indicator(self, symbol: str, timeframe: str, name: str) -> Optional[Dict[str, Any]]:
            return None
        def get_all_indicators(self, symbol: str, timeframe: str) -> Dict[str, Dict[str, Any]]:
            return {}

    regime_detector = MarketRegimeDetector()
    signal_module = SignalModule(
        indicator_source=_NullIndicatorSource(),
        regime_detector=regime_detector,
        soft_regime_enabled=True,
    )

    from src.signals.strategies.registry import register_composite_strategies
    register_composite_strategies(signal_module)

    return {
        "data_loader": data_loader,
        "signal_module": signal_module,
        "pipeline": pipeline,
        "regime_detector": regime_detector,
    }


def _extract_metrics(result_dict: Dict[str, Any]) -> Dict[str, Any]:
    """提取简化的指标摘要。"""
    m = result_dict.get("metrics", {})
    return {
        "total_trades": m.get("total_trades", 0),
        "win_rate": m.get("win_rate", 0),
        "sharpe_ratio": m.get("sharpe_ratio", 0),
        "total_pnl": m.get("total_pnl", 0),
        "max_drawdown": m.get("max_drawdown", 0),
    }
