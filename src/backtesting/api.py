"""回测 FastAPI 路由（submit/query job 模式）。

路由前缀: /v1/backtest

Job 生命周期：
  submit → pending → running → completed / failed

存储：
- BacktestJob 元数据：内存 + DB write-through（API 重启后可恢复）
- BacktestResult 详情：DB 持久化（回测完成后写入）
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

from .models import BacktestJob, BacktestJobStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/backtest", tags=["backtest"])

# Job store: run_id → BacktestJob（内存 write-through，DB 持久化）
_job_store: Dict[str, BacktestJob] = {}
_job_lock = threading.Lock()

# 并发限制：同一时刻最多 1 个回测/优化任务运行，避免耗尽 DB 连接和 CPU
_backtest_semaphore = threading.Semaphore(1)

# 回测结果缓存（仅运行中和刚完成的任务，历史数据从 DB 查询）
_result_cache: Dict[str, Dict[str, Any]] = {}
_result_cache_lock = threading.Lock()

# Walk-Forward 结果缓存（WalkForwardResult 对象，不可序列化为 dict，独立缓存）
_wf_result_cache: Dict[str, Any] = {}
_wf_result_cache_lock = threading.Lock()


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
    strategy_params_per_tf: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    # 过滤器配置
    filters_enabled: bool = True
    filter_allowed_sessions: str = "london,newyork"
    filter_session_transition_cooldown: int = 15
    filter_volatility_spike_multiplier: float = 2.5


class WalkForwardRequest(BaseModel):
    symbol: str
    timeframe: str
    start_time: str = Field(..., description="ISO 格式起始时间")
    end_time: str = Field(..., description="ISO 格式结束时间")
    strategies: Optional[List[str]] = None
    initial_balance: float = 10000.0
    min_confidence: float = 0.55
    warmup_bars: int = 200
    strategy_params: Dict[str, Any] = Field(default_factory=dict)
    strategy_params_per_tf: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    param_space: Dict[str, List[Any]] = Field(
        ..., description="参数搜索空间 {key: [val1, val2, ...]}"
    )
    search_mode: str = "grid"
    max_combinations: int = 500
    sort_metric: str = "sharpe_ratio"
    # Walk-Forward 专属参数
    n_splits: int = Field(default=5, description="滚动窗口数量")
    train_ratio: float = Field(default=0.7, description="训练集占比")
    anchored: bool = Field(default=False, description="是否使用锚定窗口")


class BacktestOptimizeRequest(BaseModel):
    symbol: str
    timeframe: str
    start_time: str
    end_time: str
    strategies: Optional[List[str]] = None
    initial_balance: float = 10000.0
    min_confidence: float = 0.55
    warmup_bars: int = 200
    strategy_params: Dict[str, Any] = Field(default_factory=dict)
    strategy_params_per_tf: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    param_space: Dict[str, List[Any]] = Field(
        ..., description="参数搜索空间 {key: [val1, val2, ...]}"
    )
    search_mode: str = "grid"
    max_combinations: int = 500
    sort_metric: str = "sharpe_ratio"


class BacktestRequestBase(BaseModel):
    symbol: str
    timeframe: str
    start_time: str = Field(..., description="ISO start time, e.g. YYYY-MM-DD")
    end_time: str = Field(..., description="ISO end time, e.g. YYYY-MM-DD")
    strategies: Optional[List[str]] = None

    initial_balance: Optional[float] = None
    min_confidence: Optional[float] = None
    warmup_bars: Optional[int] = None
    max_positions: Optional[int] = None
    commission_per_lot: Optional[float] = None
    slippage_points: Optional[float] = None
    contract_size: Optional[float] = None
    risk_percent: Optional[float] = None
    min_volume: Optional[float] = None
    max_volume: Optional[float] = None
    max_volume_per_order: Optional[float] = None
    max_volume_per_symbol: Optional[float] = None
    max_volume_per_day: Optional[float] = None
    daily_loss_limit_pct: Optional[float] = None
    max_trades_per_day: Optional[int] = None
    max_trades_per_hour: Optional[int] = None
    regime_tp_trending: Optional[float] = None
    regime_tp_ranging: Optional[float] = None
    regime_tp_breakout: Optional[float] = None
    regime_tp_uncertain: Optional[float] = None
    regime_sl_trending: Optional[float] = None
    regime_sl_ranging: Optional[float] = None
    regime_sl_breakout: Optional[float] = None
    regime_sl_uncertain: Optional[float] = None

    strategy_params: Dict[str, Any] = Field(default_factory=dict)
    strategy_params_per_tf: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    regime_affinity_overrides: Dict[str, Dict[str, float]] = Field(default_factory=dict)

    pending_entry_enabled: Optional[bool] = None
    pending_entry_pullback_atr_factor: Optional[float] = None
    pending_entry_chase_atr_factor: Optional[float] = None
    pending_entry_momentum_atr_factor: Optional[float] = None
    pending_entry_symmetric_atr_factor: Optional[float] = None
    pending_entry_expiry_bars: Optional[int] = None

    trailing_atr_multiplier: Optional[float] = None
    breakeven_atr_threshold: Optional[float] = None
    end_of_day_close_enabled: Optional[bool] = None
    end_of_day_close_hour_utc: Optional[int] = None
    end_of_day_close_minute_utc: Optional[int] = None

    filters_enabled: Optional[bool] = None
    filter_session_enabled: Optional[bool] = None
    filter_allowed_sessions: Optional[str] = None
    filter_session_transition_enabled: Optional[bool] = None
    filter_session_transition_cooldown: Optional[int] = None
    filter_volatility_enabled: Optional[bool] = None
    filter_volatility_spike_multiplier: Optional[float] = None
    filter_spread_enabled: Optional[bool] = None
    filter_max_spread_points: Optional[float] = None

    enable_regime_affinity: Optional[bool] = None
    enable_performance_tracker: Optional[bool] = None
    enable_calibrator: Optional[bool] = None
    enable_htf_alignment: Optional[bool] = None

    enable_state_machine: Optional[bool] = None
    min_preview_stable_bars: Optional[int] = None
    max_signal_evaluations: Optional[int] = None


class BacktestRunRequest(BacktestRequestBase):
    pass


class BacktestOptimizeRequest(BacktestRequestBase):
    param_space: Dict[str, List[Any]] = Field(
        ..., description="Parameter search space: {key: [val1, val2, ...]}"
    )
    search_mode: Optional[str] = None
    max_combinations: Optional[int] = None
    sort_metric: Optional[str] = None


class WalkForwardRequest(BacktestOptimizeRequest):
    n_splits: int = Field(default=5, description="Number of rolling splits")
    train_ratio: float = Field(default=0.7, description="Train set ratio")
    anchored: bool = Field(default=False, description="Whether to use anchored windows")


class BacktestJobResponse(BaseModel):
    run_id: str
    status: str
    job_type: str = "backtest"
    submitted_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    progress: float = 0.0
    error: Optional[str] = None


_CONFIG_OVERRIDE_FIELDS = (
    "initial_balance",
    "min_confidence",
    "warmup_bars",
    "max_positions",
    "commission_per_lot",
    "slippage_points",
    "contract_size",
    "risk_percent",
    "min_volume",
    "max_volume",
    "max_volume_per_order",
    "max_volume_per_symbol",
    "max_volume_per_day",
    "daily_loss_limit_pct",
    "max_trades_per_day",
    "max_trades_per_hour",
    "regime_tp_trending",
    "regime_tp_ranging",
    "regime_tp_breakout",
    "regime_tp_uncertain",
    "regime_sl_trending",
    "regime_sl_ranging",
    "regime_sl_breakout",
    "regime_sl_uncertain",
    "pending_entry_enabled",
    "pending_entry_pullback_atr_factor",
    "pending_entry_chase_atr_factor",
    "pending_entry_momentum_atr_factor",
    "pending_entry_symmetric_atr_factor",
    "pending_entry_expiry_bars",
    "trailing_atr_multiplier",
    "breakeven_atr_threshold",
    "end_of_day_close_enabled",
    "end_of_day_close_hour_utc",
    "end_of_day_close_minute_utc",
    "filters_enabled",
    "filter_session_enabled",
    "filter_allowed_sessions",
    "filter_session_transition_enabled",
    "filter_session_transition_cooldown",
    "filter_volatility_enabled",
    "filter_volatility_spike_multiplier",
    "filter_spread_enabled",
    "filter_max_spread_points",
    "enable_regime_affinity",
    "enable_performance_tracker",
    "enable_calibrator",
    "enable_htf_alignment",
    "enable_state_machine",
    "min_preview_stable_bars",
    "max_signal_evaluations",
)

_SORT_METRICS = (
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "profit_factor",
    "win_rate",
    "total_pnl",
    "expectancy",
)

_SEARCH_MODES = ("grid", "random")


def _load_backtest_defaults() -> Dict[str, Any]:
    from .config import get_backtest_defaults

    return get_backtest_defaults()


def _load_signal_config() -> Any:
    from src.config.signal import get_signal_config

    return get_signal_config()


_PARAM_TEMPLATE_SPECS: Dict[str, Dict[str, Any]] = {
    "supertrend__adx_threshold": {"default": 23.0, "step": 2.0, "min": 18.0, "max": 30.0, "precision": 1},
    "roc_momentum__adx_min": {"default": 23.0, "step": 2.0, "min": 18.0, "max": 30.0, "precision": 1},
    "roc_momentum__roc_threshold": {"default": 0.10, "step": 0.02, "min": 0.05, "max": 0.30, "precision": 2},
    "donchian_breakout__adx_min": {"default": 23.0, "step": 2.0, "min": 18.0, "max": 30.0, "precision": 1},
    "rsi_reversion__overbought": {"default": 70.0, "step": 3.0, "min": 65.0, "max": 85.0, "precision": 0},
    "rsi_reversion__oversold": {"default": 30.0, "step": 3.0, "min": 15.0, "max": 35.0, "precision": 0},
    "williams_r__overbought": {"default": -20.0, "step": 5.0, "min": -35.0, "max": -10.0, "precision": 0},
    "williams_r__oversold": {"default": -80.0, "step": 5.0, "min": -90.0, "max": -65.0, "precision": 0},
    "cci_reversion__upper_threshold": {"default": 100.0, "step": 20.0, "min": 100.0, "max": 220.0, "precision": 0},
    "cci_reversion__lower_threshold": {"default": -100.0, "step": 20.0, "min": -220.0, "max": -100.0, "precision": 0},
    "stoch_rsi__overbought": {"default": 80.0, "step": 5.0, "min": 70.0, "max": 90.0, "precision": 0},
    "stoch_rsi__oversold": {"default": 20.0, "step": 5.0, "min": 10.0, "max": 30.0, "precision": 0},
    "rsi_reversion__intrabar_decay": {"default": 0.90, "step": 0.04, "min": 0.70, "max": 0.95, "precision": 2},
    "stoch_rsi__intrabar_decay": {"default": 0.88, "step": 0.04, "min": 0.70, "max": 0.95, "precision": 2},
    "williams_r__intrabar_decay": {"default": 0.88, "step": 0.04, "min": 0.70, "max": 0.95, "precision": 2},
    "cci_reversion__intrabar_decay": {"default": 0.88, "step": 0.04, "min": 0.70, "max": 0.95, "precision": 2},
    "bollinger_breakout__intrabar_decay": {"default": 0.78, "step": 0.04, "min": 0.65, "max": 0.90, "precision": 2},
    "keltner_bb_squeeze__intrabar_decay": {"default": 0.80, "step": 0.04, "min": 0.65, "max": 0.90, "precision": 2},
    "session_momentum__london_min_atr_pct": {
        "default": 0.00050, "step": 0.00006, "min": 0.00020, "max": 0.00080, "precision": 5,
    },
    "session_momentum__other_min_atr_pct": {
        "default": 0.00038, "step": 0.00005, "min": 0.00015, "max": 0.00065, "precision": 5,
    },
}


def _normalize_strategy_name_list(raw_values: Optional[List[str]]) -> List[str]:
    normalized: List[str] = []
    seen = set()
    for raw in raw_values or []:
        value = str(raw).strip()
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized


def _collect_configured_strategies(signal_config: Any) -> List[str]:
    ordered: List[str] = []
    seen = set()

    def add(name: str) -> None:
        value = str(name).strip()
        if not value or value in seen:
            return
        ordered.append(value)
        seen.add(value)

    for name in getattr(signal_config, "strategy_timeframes", {}).keys():
        add(name)
    for compound_key in getattr(signal_config, "strategy_params", {}).keys():
        add(str(compound_key).split("__", 1)[0])
    for tf_bucket in getattr(signal_config, "strategy_params_per_tf", {}).values():
        for compound_key in tf_bucket.keys():
            add(str(compound_key).split("__", 1)[0])
    for name in getattr(signal_config, "regime_affinity_overrides", {}).keys():
        add(name)
    return ordered


def _resolve_template_strategies(
    signal_config: Any,
    timeframe: str,
    requested: Optional[List[str]] = None,
) -> List[str]:
    requested_list = _normalize_strategy_name_list(requested)
    candidates = requested_list or _collect_configured_strategies(signal_config)
    timeframe_upper = timeframe.upper()
    resolved: List[str] = []
    seen = set()
    strategy_timeframes = getattr(signal_config, "strategy_timeframes", {})

    for strategy in candidates:
        if strategy in seen:
            continue
        allowed = [
            str(tf).strip().upper()
            for tf in strategy_timeframes.get(strategy, [])
            if str(tf).strip()
        ]
        if allowed and timeframe_upper not in allowed:
            continue
        resolved.append(strategy)
        seen.add(strategy)
    return resolved


def _effective_strategy_params_for_timeframe(
    signal_config: Any,
    timeframe: str,
) -> Dict[str, float]:
    merged = {
        str(key): float(value)
        for key, value in getattr(signal_config, "strategy_params", {}).items()
    }
    timeframe_bucket = getattr(signal_config, "strategy_params_per_tf", {}).get(
        timeframe.upper(), {}
    )
    for key, value in timeframe_bucket.items():
        merged[str(key)] = float(value)
    return merged


def _generate_template_values(
    compound_key: str,
    current_value: Optional[float],
) -> List[float]:
    spec = _PARAM_TEMPLATE_SPECS.get(compound_key)
    if spec is None:
        if current_value is None:
            return []
        magnitude = abs(float(current_value))
        if magnitude >= 1:
            step = max(1.0, round(magnitude * 0.1))
            candidates = [
                current_value - 2 * step,
                current_value - step,
                current_value,
                current_value + step,
                current_value + 2 * step,
            ]
            return sorted({round(float(v), 2) for v in candidates})
        step = max(0.01, round(max(magnitude * 0.15, 0.01), 2))
        candidates = [
            max(0.0, current_value - 2 * step),
            max(0.0, current_value - step),
            current_value,
            current_value + step,
            current_value + 2 * step,
        ]
        return sorted({round(float(v), 4) for v in candidates})

    base = float(current_value if current_value is not None else spec["default"])
    step = float(spec["step"])
    min_value = float(spec["min"])
    max_value = float(spec["max"])
    precision = int(spec["precision"])
    candidates = [
        max(min_value, min(max_value, base - 2 * step)),
        max(min_value, min(max_value, base - step)),
        max(min_value, min(max_value, base)),
        max(min_value, min(max_value, base + step)),
        max(min_value, min(max_value, base + 2 * step)),
    ]
    return sorted({round(value, precision) for value in candidates})


def _build_param_space_template(
    timeframe: str,
    requested_strategies: Optional[List[str]] = None,
) -> Dict[str, Any]:
    signal_config = _load_signal_config()
    resolved_strategies = _resolve_template_strategies(
        signal_config,
        timeframe,
        requested_strategies,
    )
    effective_strategy_params = _effective_strategy_params_for_timeframe(
        signal_config,
        timeframe,
    )

    baseline_strategy_params: Dict[str, float] = {}
    param_space: Dict[str, List[float]] = {}
    for strategy in resolved_strategies:
        strategy_keys = [
            key for key in effective_strategy_params.keys()
            if key.startswith(f"{strategy}__")
        ]
        for compound_key in strategy_keys:
            current_value = effective_strategy_params.get(compound_key)
            if current_value is not None:
                baseline_strategy_params[compound_key] = current_value
            values = _generate_template_values(compound_key, current_value)
            if values:
                param_space[compound_key] = values

    notes = [
        "param_space 基于 signal.ini 当前生效策略参数生成，可继续在前端编辑。",
        "未显式选择 strategies 时，会按当前 timeframe 自动筛选可运行策略。",
    ]
    if not param_space:
        notes.append("当前所选策略没有已配置的 strategy_params 模板，请手动补充 param_space。")

    return {
        "timeframe": timeframe.upper(),
        "requested_strategies": _normalize_strategy_name_list(requested_strategies),
        "resolved_strategies": resolved_strategies,
        "baseline_strategy_params": baseline_strategy_params,
        "param_space": param_space,
        "notes": notes,
    }


def _parse_request_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _build_backtest_config(request: BacktestRequestBase) -> Any:
    from src.backtesting.models import BacktestConfig

    defaults = _load_backtest_defaults()
    config_kwargs: Dict[str, Any] = {
        "symbol": request.symbol,
        "timeframe": request.timeframe,
        "start_time": _parse_request_datetime(request.start_time),
        "end_time": _parse_request_datetime(request.end_time),
        "strategies": request.strategies,
        "strategy_params": request.strategy_params,
        "strategy_params_per_tf": request.strategy_params_per_tf,
        "regime_affinity_overrides": request.regime_affinity_overrides,
    }
    for field_name in _CONFIG_OVERRIDE_FIELDS:
        value = getattr(request, field_name, None)
        if value is not None:
            config_kwargs[field_name] = value
        elif field_name in defaults:
            config_kwargs[field_name] = defaults[field_name]
    return BacktestConfig(**config_kwargs)


def _resolve_optimizer_settings(request: BacktestOptimizeRequest) -> Dict[str, Any]:
    defaults = _load_backtest_defaults()
    return {
        "search_mode": request.search_mode or defaults.get("search_mode", "grid"),
        "max_combinations": (
            request.max_combinations
            if request.max_combinations is not None
            else defaults.get("max_combinations", 500)
        ),
        "sort_metric": request.sort_metric or defaults.get("sort_metric", "sharpe_ratio"),
    }


# ── 路由实现 ────────────────────────────────────────────────────


@router.post("/run", response_model=ApiResponse)
async def run_backtest(
    request: BacktestRunRequest,
    background_tasks: BackgroundTasks,
) -> ApiResponse:
    """提交单次回测任务。"""
    run_id = f"bt_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    job = BacktestJob(
        run_id=run_id,
        job_type="backtest",
        status=BacktestJobStatus.PENDING,
        submitted_at=now,
        config_summary={
            "symbol": request.symbol,
            "timeframe": request.timeframe,
            "start_time": request.start_time,
            "end_time": request.end_time,
        },
    )
    _register_job(job)
    background_tasks.add_task(_execute_backtest, run_id, request)
    return ApiResponse(success=True, data=job.to_dict())


@router.post("/optimize", response_model=ApiResponse)
async def run_optimization(
    request: BacktestOptimizeRequest,
    background_tasks: BackgroundTasks,
) -> ApiResponse:
    """提交参数优化任务。"""
    optimizer_settings = _resolve_optimizer_settings(request)
    run_id = f"opt_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    job = BacktestJob(
        run_id=run_id,
        job_type="optimization",
        status=BacktestJobStatus.PENDING,
        submitted_at=now,
        config_summary={
            "symbol": request.symbol,
            "timeframe": request.timeframe,
            "start_time": request.start_time,
            "end_time": request.end_time,
            "search_mode": optimizer_settings["search_mode"],
            "max_combinations": optimizer_settings["max_combinations"],
        },
    )
    _register_job(job)
    background_tasks.add_task(_execute_optimization, run_id, request)
    return ApiResponse(success=True, data=job.to_dict())


@router.post("/walk-forward", response_model=ApiResponse)
async def run_walk_forward(
    request: WalkForwardRequest,
    background_tasks: BackgroundTasks,
) -> ApiResponse:
    """提交 Walk-Forward 验证任务。"""
    optimizer_settings = _resolve_optimizer_settings(request)
    run_id = f"wf_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    job = BacktestJob(
        run_id=run_id,
        job_type="walk_forward",
        status=BacktestJobStatus.PENDING,
        submitted_at=now,
        config_summary={
            "symbol": request.symbol,
            "timeframe": request.timeframe,
            "start_time": request.start_time,
            "end_time": request.end_time,
            "n_splits": request.n_splits,
            "train_ratio": request.train_ratio,
            "search_mode": optimizer_settings["search_mode"],
            "max_combinations": optimizer_settings["max_combinations"],
        },
    )
    _register_job(job)
    background_tasks.add_task(_execute_walk_forward, run_id, request)
    return ApiResponse(success=True, data=job.to_dict())


@router.get("/config/defaults", response_model=ApiResponse)
async def get_backtest_config_defaults() -> ApiResponse:
    """Return resolved backtest defaults and API-exposed capabilities."""
    defaults = _load_backtest_defaults()
    return ApiResponse(
        success=True,
        data={
            "defaults": defaults,
            "supported": {
                "search_modes": list(_SEARCH_MODES),
                "sort_metrics": list(_SORT_METRICS),
                "run_fields": [
                    "symbol",
                    "timeframe",
                    "start_time",
                    "end_time",
                    "strategies",
                    *_CONFIG_OVERRIDE_FIELDS,
                    "strategy_params",
                    "strategy_params_per_tf",
                    "regime_affinity_overrides",
                ],
                "optimize_fields": [
                    "param_space",
                    "search_mode",
                    "max_combinations",
                    "sort_metric",
                ],
                "walk_forward_fields": [
                    "n_splits",
                    "train_ratio",
                    "anchored",
                ],
            },
        },
    )


@router.get("/config/param-space-template", response_model=ApiResponse)
async def get_backtest_param_space_template(
    timeframe: str,
    strategies: Optional[str] = None,
) -> ApiResponse:
    """Return a strategy-aware param_space template for optimize/WF jobs."""
    requested = None
    if strategies:
        requested = [
            item.strip()
            for item in strategies.split(",")
            if item.strip()
        ]
    return ApiResponse(
        success=True,
        data=_build_param_space_template(timeframe, requested),
    )


@router.get("/jobs", response_model=ApiResponse)
async def list_jobs() -> ApiResponse:
    """列出所有任务状态（内存中的活跃任务）。"""
    with _job_lock:
        jobs = [job.to_dict() for job in _job_store.values()]
    return ApiResponse(success=True, data=jobs)


@router.get("/jobs/{run_id}", response_model=ApiResponse)
async def get_job_status(run_id: str) -> ApiResponse:
    """查询任务状态。"""
    with _job_lock:
        job = _job_store.get(run_id)
    if job is not None:
        return ApiResponse(success=True, data=job.to_dict())
    return ApiResponse(success=False, error=f"Job {run_id} not found")


@router.get("/results/{run_id}", response_model=ApiResponse)
async def get_result(run_id: str) -> ApiResponse:
    """获取回测结果详情（优先内存缓存，回退 DB）。"""
    with _result_cache_lock:
        cached = _result_cache.get(run_id)
    if cached is not None:
        return ApiResponse(success=True, data=cached)

    # 回退到 DB 查询
    try:
        repo = _get_backtest_repo()
        if repo is not None:
            db_result = repo.fetch_run(run_id)
            if db_result is not None:
                return ApiResponse(success=True, data=db_result)
    except Exception:
        logger.debug("DB fallback query failed for %s", run_id, exc_info=True)

    # 检查 job 是否还在运行
    with _job_lock:
        job = _job_store.get(run_id)
    if job is not None:
        if job.status == BacktestJobStatus.RUNNING:
            return ApiResponse(
                success=True,
                data={"run_id": run_id, "status": "running", "progress": job.progress},
            )
        if job.status == BacktestJobStatus.FAILED:
            return ApiResponse(success=False, error=job.error or "Unknown error")
        if job.status == BacktestJobStatus.PENDING:
            return ApiResponse(
                success=True,
                data={"run_id": run_id, "status": "pending"},
            )

    return ApiResponse(success=False, error=f"Run {run_id} not found")


# 保留旧端点别名（向后兼容）——返回结果摘要而非原始 job 元数据
@router.get("/results", response_model=ApiResponse)
async def list_results() -> ApiResponse:
    """列出所有已完成任务的结果摘要（兼容旧 API）。"""
    summaries: list[Dict[str, Any]] = []
    with _job_lock:
        jobs = list(_job_store.values())
    for job in jobs:
        entry: Dict[str, Any] = {
            "run_id": job.run_id,
            "type": job.job_type,
            "status": job.status.value,
            "submitted_at": job.submitted_at.isoformat(),
            "completed_at": (
                job.completed_at.isoformat() if job.completed_at else None
            ),
            "config_summary": job.config_summary,
        }
        with _result_cache_lock:
            cached = _result_cache.get(job.run_id)
        if cached is not None:
            entry["metrics"] = _extract_result_metrics(cached, job.job_type)
        summaries.append(entry)
    return ApiResponse(success=True, data=summaries)


def _extract_result_metrics(
    cached: Any, job_type: str,
) -> Dict[str, Any]:
    """从缓存的结果中提取关键指标摘要。"""
    if job_type == "optimization" and isinstance(cached, list):
        return {
            "optimization_count": len(cached),
            "best": _pick_metrics(cached[0]) if cached else {},
        }
    if isinstance(cached, dict):
        return _pick_metrics(cached)
    return {}


def _pick_metrics(result: Dict[str, Any]) -> Dict[str, Any]:
    """从单个 BacktestResult dict 中挑选关键指标。"""
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


@router.get("/history", response_model=ApiResponse)
async def list_history(limit: int = 50, offset: int = 0) -> ApiResponse:
    """查询历史回测结果列表（从 DB）。"""
    try:
        repo = _get_backtest_repo()
        if repo is None:
            return ApiResponse(success=False, error="Database not available")
        runs = repo.fetch_runs(limit=limit, offset=offset)
        return ApiResponse(success=True, data=runs)
    except Exception as e:
        return ApiResponse(success=False, error=str(e))


@router.get("/history/{run_id}/trades", response_model=ApiResponse)
async def get_trades(run_id: str) -> ApiResponse:
    """查询某次回测的交易明细（从 DB）。"""
    try:
        repo = _get_backtest_repo()
        if repo is None:
            return ApiResponse(success=False, error="Database not available")
        trades = repo.fetch_trades(run_id)
        return ApiResponse(success=True, data=trades)
    except Exception as e:
        return ApiResponse(success=False, error=str(e))


@router.get("/history/{run_id}/evaluations", response_model=ApiResponse)
async def get_evaluations(
    run_id: str,
    strategy: Optional[str] = None,
    filtered_only: bool = False,
    limit: int = 1000,
) -> ApiResponse:
    """查询信号评估明细（从 DB）。"""
    try:
        repo = _get_backtest_repo()
        if repo is None:
            return ApiResponse(success=False, error="Database not available")
        evals = repo.fetch_evaluations(
            run_id, strategy=strategy, filtered_only=filtered_only, limit=limit
        )
        return ApiResponse(success=True, data=evals)
    except Exception as e:
        return ApiResponse(success=False, error=str(e))


@router.get("/history/{run_id}/evaluation-summary", response_model=ApiResponse)
async def get_evaluation_summary(run_id: str) -> ApiResponse:
    """查询信号评估汇总统计（从 DB）。"""
    try:
        repo = _get_backtest_repo()
        if repo is None:
            return ApiResponse(success=False, error="Database not available")
        summary = repo.fetch_evaluation_summary(run_id)
        return ApiResponse(success=True, data=summary)
    except Exception as e:
        return ApiResponse(success=False, error=str(e))


# ── 后台执行函数 ────────────────────────────────────────────────


def _execute_backtest(run_id: str, request: BacktestRunRequest) -> None:
    """在后台线程执行回测。"""
    acquired = _backtest_semaphore.acquire(timeout=5)
    if not acquired:
        _fail_job(run_id, "另一个回测/优化任务正在执行，请稍后重试")
        return

    _start_job(run_id)
    components: Optional[Dict[str, Any]] = None
    try:
        from src.backtesting.engine import BacktestEngine

        config = _build_backtest_config(request)

        components = _build_api_components(
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
            voting_engine=components.get("voting_engine"),
        )

        result = engine.run()
        result_dict = result.to_dict()

        _persist_result(result)
        _complete_job(run_id, result_dict)

    except Exception as e:
        logger.exception("Backtest %s failed", run_id)
        _fail_job(run_id, str(e))
    finally:
        _cleanup_components(components)
        _backtest_semaphore.release()


def _execute_optimization(run_id: str, request: BacktestOptimizeRequest) -> None:
    """在后台线程执行参数优化。"""
    acquired = _backtest_semaphore.acquire(timeout=5)
    if not acquired:
        _fail_job(run_id, "另一个回测/优化任务正在执行，请稍后重试")
        return

    _start_job(run_id)
    components: Optional[Dict[str, Any]] = None
    try:
        from src.backtesting.models import ParameterSpace
        from src.backtesting.optimizer import (
            ParameterOptimizer,
            build_signal_module_with_overrides,
        )

        optimizer_settings = _resolve_optimizer_settings(request)
        config = _build_backtest_config(request)

        param_space = ParameterSpace(
            strategy_params=request.param_space,
            search_mode=optimizer_settings["search_mode"],
            max_combinations=optimizer_settings["max_combinations"],
        )

        components = _build_api_components(
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
            voting_engine=components.get("voting_engine"),
            sort_metric=optimizer_settings["sort_metric"],
        )

        results = optimizer.run()
        for r in results:
            _persist_result(r)

        results_dicts = [r.to_dict() for r in results[:50]]
        _complete_job(run_id, results_dicts)

    except Exception as e:
        logger.exception("Optimization %s failed", run_id)
        _fail_job(run_id, str(e))
    finally:
        _cleanup_components(components)
        _backtest_semaphore.release()


def _execute_walk_forward(run_id: str, request: WalkForwardRequest) -> None:
    """在后台线程执行 Walk-Forward 验证。"""
    acquired = _backtest_semaphore.acquire(timeout=5)
    if not acquired:
        _fail_job(run_id, "另一个回测/优化任务正在执行，请稍后重试")
        return

    _start_job(run_id)
    components: Optional[Dict[str, Any]] = None
    try:
        from src.backtesting.models import ParameterSpace
        from src.backtesting.optimizer import build_signal_module_with_overrides
        from src.backtesting.walk_forward import WalkForwardConfig, WalkForwardValidator

        optimizer_settings = _resolve_optimizer_settings(request)
        base_config = _build_backtest_config(request)

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

        components = _build_api_components(
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
            voting_engine=components.get("voting_engine"),
        )

        wf_result = validator.run()

        # 缓存 WalkForwardResult 对象（供推荐引擎使用）
        with _wf_result_cache_lock:
            _wf_result_cache[run_id] = wf_result

        # 持久化各窗口的 OOS 结果
        for split in wf_result.splits:
            _persist_result(split.out_of_sample_result)

        # 序列化摘要给 API 查询
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
                    "split_index": s.split_index,
                    "best_params": s.best_params,
                    "in_sample_sharpe": s.in_sample_result.metrics.sharpe_ratio,
                    "out_of_sample_sharpe": s.out_of_sample_result.metrics.sharpe_ratio,
                }
                for s in wf_result.splits
            ],
        }
        _complete_job(run_id, summary)

    except Exception as e:
        logger.exception("Walk-Forward %s failed", run_id)
        _fail_job(run_id, str(e))
    finally:
        _cleanup_components(components)
        _backtest_semaphore.release()


# ── Job 状态管理 ─────────────────────────────────────────────────


def _register_job(job: BacktestJob) -> None:
    """注册新任务到内存 store。"""
    with _job_lock:
        _job_store[job.run_id] = job


def _start_job(run_id: str) -> None:
    """标记任务开始执行。"""
    with _job_lock:
        job = _job_store.get(run_id)
        if job is not None:
            job.status = BacktestJobStatus.RUNNING
            job.started_at = datetime.now(timezone.utc)


def _complete_job(run_id: str, result: Any) -> None:
    """标记任务完成并缓存结果。"""
    now = datetime.now(timezone.utc)
    with _job_lock:
        job = _job_store.get(run_id)
        if job is not None:
            job.status = BacktestJobStatus.COMPLETED
            job.completed_at = now
            job.progress = 1.0
    with _result_cache_lock:
        _result_cache[run_id] = result


def _fail_job(run_id: str, error: str) -> None:
    """标记任务失败。"""
    now = datetime.now(timezone.utc)
    with _job_lock:
        job = _job_store.get(run_id)
        if job is not None:
            job.status = BacktestJobStatus.FAILED
            job.completed_at = now
            job.error = error


# ── 组件管理 ──────────────────────────────────────────────────────


def _cleanup_components(components: Optional[Dict[str, Any]]) -> None:
    """回测完成后释放独立 pipeline 和 DB 连接资源。"""
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


def _persist_result(result: Any) -> None:
    """将回测结果持久化到 DB（best-effort，失败不影响主流程）。"""
    try:
        repo = _get_backtest_repo()
        if repo is not None:
            repo.save_result(result)
    except Exception:
        logger.warning(
            "Failed to persist backtest result %s", result.run_id, exc_info=True
        )


_cached_backtest_repo: Optional[Any] = None


def _get_backtest_repo() -> Optional[Any]:
    """获取 BacktestRepository 实例（懒加载 + 模块级缓存，独立连接池）。"""
    global _cached_backtest_repo
    if _cached_backtest_repo is not None:
        return _cached_backtest_repo
    try:
        from src.config.database import get_db_config
        from src.persistence.db import TimescaleWriter
        from src.persistence.repositories.backtest_repo import BacktestRepository

        db_config = get_db_config()
        writer = TimescaleWriter(settings=db_config, min_conn=1, max_conn=2)
        repo = BacktestRepository(writer)
        repo.ensure_schema()
        _cached_backtest_repo = repo
        return repo
    except Exception:
        logger.debug("BacktestRepository not available", exc_info=True)
        return None


def _build_api_components(
    strategy_params: Optional[Dict[str, Any]] = None,
    strategy_params_per_tf: Optional[Dict[str, Dict[str, Any]]] = None,
    regime_affinity_overrides: Optional[Dict[str, Dict[str, float]]] = None,
) -> Dict[str, Any]:
    """构建回测所需组件（委托给共享工厂）。"""
    from .component_factory import build_backtest_components

    return build_backtest_components(
        strategy_params=strategy_params,
        strategy_params_per_tf=strategy_params_per_tf,
        regime_affinity_overrides=regime_affinity_overrides,
    )


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


# ── 参数推荐 API ─────────────────────────────────────────────────────

# 内存缓存（rec_id → Recommendation），DB write-through
_rec_cache: Dict[str, Any] = {}
_rec_cache_lock = threading.Lock()


class GenerateRecommendationRequest(BaseModel):
    walk_forward_run_id: str = Field(..., description="Walk-Forward 验证的 run_id")


class ApproveRejectRequest(BaseModel):
    reason: str = Field(default="", description="审核理由（可选）")


@router.post("/recommendations/generate", response_model=ApiResponse)
async def generate_recommendation(
    request: GenerateRecommendationRequest,
) -> ApiResponse:
    """从 Walk-Forward 验证结果生成参数推荐。

    前置条件：Walk-Forward 验证已完成且结果可查。
    """
    try:
        wf_result = _load_walk_forward_result(request.walk_forward_run_id)
        if wf_result is None:
            return ApiResponse(
                success=False,
                error=f"Walk-Forward 结果 {request.walk_forward_run_id} 未找到",
            )

        from src.backtesting.recommendation import (
            RecommendationEngine,
            load_current_signal_config,
        )

        wf_timeframe = getattr(
            getattr(getattr(wf_result, "config", None), "base_config", None),
            "timeframe",
            None,
        )
        current_params, _, current_affinities = load_current_signal_config(
            wf_timeframe
        )
        engine = RecommendationEngine()
        rec = engine.generate(
            wf_result=wf_result,
            source_run_id=request.walk_forward_run_id,
            current_strategy_params=current_params,
            current_regime_affinities=current_affinities,
            timeframe=wf_timeframe,
        )

        # 持久化
        repo = _get_backtest_repo()
        if repo is not None:
            repo.save_recommendation(rec)

        with _rec_cache_lock:
            _rec_cache[rec.rec_id] = rec

        return ApiResponse(success=True, data=rec.to_dict())

    except ValueError as e:
        return ApiResponse(success=False, error=str(e))
    except Exception as e:
        logger.exception("Failed to generate recommendation")
        return ApiResponse(success=False, error=str(e))


@router.get("/recommendations", response_model=ApiResponse)
async def list_recommendations(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> ApiResponse:
    """列出参数推荐记录（支持 status 筛选）。"""
    try:
        repo = _get_backtest_repo()
        if repo is None:
            return ApiResponse(success=False, error="Database not available")
        recs = repo.fetch_recommendations(status=status, limit=limit, offset=offset)
        return ApiResponse(success=True, data=[r.to_dict() for r in recs])
    except Exception as e:
        return ApiResponse(success=False, error=str(e))


@router.get("/recommendations/{rec_id}", response_model=ApiResponse)
async def get_recommendation(rec_id: str) -> ApiResponse:
    """查看推荐详情（含参数 diff）。"""
    rec = _get_recommendation(rec_id)
    if rec is None:
        return ApiResponse(success=False, error=f"推荐 {rec_id} 未找到")
    return ApiResponse(success=True, data=rec.to_dict())


@router.post("/recommendations/{rec_id}/approve", response_model=ApiResponse)
async def approve_recommendation(
    rec_id: str,
    request: ApproveRejectRequest,
) -> ApiResponse:
    """审核通过参数推荐。"""
    from src.backtesting.models import RecommendationStatus

    rec = _get_recommendation(rec_id)
    if rec is None:
        return ApiResponse(success=False, error=f"推荐 {rec_id} 未找到")
    if rec.status != RecommendationStatus.PENDING:
        return ApiResponse(
            success=False,
            error=f"推荐状态为 {rec.status.value}，仅 pending 可审核",
        )

    rec.status = RecommendationStatus.APPROVED
    rec.approved_at = datetime.now(timezone.utc)

    repo = _get_backtest_repo()
    if repo is not None:
        repo.update_recommendation(rec)

    with _rec_cache_lock:
        _rec_cache[rec_id] = rec

    return ApiResponse(success=True, data=rec.to_dict())


@router.post("/recommendations/{rec_id}/reject", response_model=ApiResponse)
async def reject_recommendation(
    rec_id: str,
    request: ApproveRejectRequest,
) -> ApiResponse:
    """审核拒绝参数推荐。"""
    from src.backtesting.models import RecommendationStatus

    rec = _get_recommendation(rec_id)
    if rec is None:
        return ApiResponse(success=False, error=f"推荐 {rec_id} 未找到")
    if rec.status != RecommendationStatus.PENDING:
        return ApiResponse(
            success=False,
            error=f"推荐状态为 {rec.status.value}，仅 pending 可拒绝",
        )

    rec.status = RecommendationStatus.REJECTED
    repo = _get_backtest_repo()
    if repo is not None:
        repo.update_recommendation(rec)

    with _rec_cache_lock:
        _rec_cache[rec_id] = rec

    return ApiResponse(success=True, data=rec.to_dict())


@router.post("/recommendations/{rec_id}/apply", response_model=ApiResponse)
async def apply_recommendation(rec_id: str) -> ApiResponse:
    """应用已审核通过的推荐（写入 signal.local.ini + 内存热更新）。"""
    from src.backtesting.recommendation import ConfigApplicator

    rec = _get_recommendation(rec_id)
    if rec is None:
        return ApiResponse(success=False, error=f"推荐 {rec_id} 未找到")

    try:
        signal_module = _get_signal_module()
        applicator = ConfigApplicator(signal_module=signal_module)
        backup_path = applicator.apply(rec)

        # DB 持久化（失败时记录警告但不回滚文件操作——
        # 配置已生效，DB 状态可通过下次查询时自愈）
        repo = _get_backtest_repo()
        if repo is not None:
            try:
                repo.update_recommendation(rec)
            except Exception:
                logger.warning(
                    "推荐 %s 已应用但 DB 更新失败，状态可能不一致",
                    rec_id,
                    exc_info=True,
                )

        with _rec_cache_lock:
            _rec_cache[rec_id] = rec

        return ApiResponse(
            success=True,
            data={**rec.to_dict(), "backup_path": backup_path},
        )
    except ValueError as e:
        return ApiResponse(success=False, error=str(e))
    except Exception as e:
        logger.exception("Failed to apply recommendation %s", rec_id)
        return ApiResponse(success=False, error=str(e))


@router.post("/recommendations/{rec_id}/rollback", response_model=ApiResponse)
async def rollback_recommendation(rec_id: str) -> ApiResponse:
    """回滚已应用的推荐。"""
    from src.backtesting.recommendation import ConfigApplicator

    rec = _get_recommendation(rec_id)
    if rec is None:
        return ApiResponse(success=False, error=f"推荐 {rec_id} 未找到")

    try:
        signal_module = _get_signal_module()
        applicator = ConfigApplicator(signal_module=signal_module)
        applicator.rollback(rec)

        repo = _get_backtest_repo()
        if repo is not None:
            try:
                repo.update_recommendation(rec)
            except Exception:
                logger.warning(
                    "推荐 %s 已回滚但 DB 更新失败",
                    rec_id,
                    exc_info=True,
                )

        with _rec_cache_lock:
            _rec_cache[rec_id] = rec

        return ApiResponse(success=True, data=rec.to_dict())
    except ValueError as e:
        return ApiResponse(success=False, error=str(e))
    except Exception as e:
        logger.exception("Failed to rollback recommendation %s", rec_id)
        return ApiResponse(success=False, error=str(e))


# ── 推荐 helper ──────────────────────────────────────────────────────


def _get_recommendation(rec_id: str) -> Optional[Any]:
    """获取推荐记录（优先内存缓存，回退 DB）。"""
    with _rec_cache_lock:
        cached = _rec_cache.get(rec_id)
    if cached is not None:
        return cached

    repo = _get_backtest_repo()
    if repo is not None:
        rec = repo.fetch_recommendation(rec_id)
        if rec is not None:
            with _rec_cache_lock:
                _rec_cache[rec_id] = rec
            return rec
    return None


def _load_walk_forward_result(run_id: str) -> Optional[Any]:
    """从 Walk-Forward 专用缓存加载结果。

    WalkForwardResult 对象（含 splits、BacktestResult 等）无法从 DB 重建，
    因此仅在当前进程内有效。API 重启后需重新运行 Walk-Forward 验证。
    """
    with _wf_result_cache_lock:
        return _wf_result_cache.get(run_id)


def _get_signal_module() -> Optional[Any]:
    """尝试获取运行时的 SignalModule 实例。"""
    try:
        from src.api.deps import get_signal_service

        return get_signal_service()
    except Exception:
        logger.debug("SignalModule not available (standalone backtest mode)")
        return None
