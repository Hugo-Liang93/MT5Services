from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

CONFIG_OVERRIDE_FIELDS = (
    "initial_balance",
    "simulation_mode",
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
    "htf_alignment_boost",
    "htf_conflict_penalty",
    "bars_to_evaluate",
    "enable_state_machine",
    "min_preview_stable_bars",
    "max_signal_evaluations",
)

SORT_METRICS = (
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "profit_factor",
    "win_rate",
    "total_pnl",
    "expectancy",
)

SEARCH_MODES = ("grid", "random")


class BacktestRequestBase(BaseModel):
    symbol: str
    timeframe: str
    start_time: str = Field(..., description="ISO start time, e.g. YYYY-MM-DD")
    end_time: str = Field(..., description="ISO end time, e.g. YYYY-MM-DD")
    strategies: Optional[List[str]] = None

    initial_balance: Optional[float] = None
    simulation_mode: Optional[str] = None
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
    htf_alignment_boost: Optional[float] = None
    htf_conflict_penalty: Optional[float] = None
    bars_to_evaluate: Optional[int] = None

    enable_state_machine: Optional[bool] = None
    min_preview_stable_bars: Optional[int] = None
    max_signal_evaluations: Optional[int] = None

    # 实验追踪 ID（跨 Research/Backtest/PaperTrading 关联）
    experiment_id: Optional[str] = None


class BacktestRunRequest(BacktestRequestBase):
    reason: str = "manual_backtest_run"
    actor: str = "operator"
    idempotency_key: Optional[str] = None
    request_context: Dict[str, Any] = Field(default_factory=dict)


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


class BacktestRunActionView(BaseModel):
    accepted: bool
    status: str
    action_id: str
    audit_id: Optional[str] = None
    actor: Optional[str] = None
    reason: Optional[str] = None
    idempotency_key: Optional[str] = None
    request_context: Dict[str, Any] = Field(default_factory=dict)
    message: Optional[str] = None
    error_code: Optional[str] = None
    recorded_at: Optional[str] = None
    effective_state: Dict[str, Any] = Field(default_factory=dict)
    run_id: str
    job: Dict[str, Any] = Field(default_factory=dict)


def load_backtest_defaults() -> Dict[str, Any]:
    from src.backtesting.config import get_backtest_defaults

    return get_backtest_defaults()


def load_signal_config() -> Any:
    from src.config.signal import get_signal_config

    return get_signal_config()


def strategy_scope_overrides(signal_config: Any) -> Dict[str, Dict[str, list[str]]]:
    def _normalize(mapping: Any) -> Dict[str, list[str]]:
        normalized: Dict[str, list[str]] = {}
        for name, values in (mapping or {}).items():
            items = list(values) if isinstance(values, (list, tuple, set)) else [values]
            cleaned = [str(item).strip() for item in items if str(item).strip()]
            if cleaned:
                normalized[str(name)] = cleaned
        return normalized

    return {
        "strategy_timeframes": _normalize(signal_config.strategy_timeframes),
        "strategy_sessions": _normalize(signal_config.strategy_sessions),
    }


_PARAM_TEMPLATE_SPECS: Dict[str, Dict[str, Any]] = {
    "supertrend__adx_threshold": {
        "default": 23.0,
        "step": 2.0,
        "min": 18.0,
        "max": 30.0,
        "precision": 1,
    },
    "roc_momentum__adx_min": {
        "default": 23.0,
        "step": 2.0,
        "min": 18.0,
        "max": 30.0,
        "precision": 1,
    },
    "roc_momentum__roc_threshold": {
        "default": 0.10,
        "step": 0.02,
        "min": 0.05,
        "max": 0.30,
        "precision": 2,
    },
    "donchian_breakout__adx_min": {
        "default": 23.0,
        "step": 2.0,
        "min": 18.0,
        "max": 30.0,
        "precision": 1,
    },
    "rsi_reversion__overbought": {
        "default": 70.0,
        "step": 3.0,
        "min": 65.0,
        "max": 85.0,
        "precision": 0,
    },
    "rsi_reversion__oversold": {
        "default": 30.0,
        "step": 3.0,
        "min": 15.0,
        "max": 35.0,
        "precision": 0,
    },
    "williams_r__overbought": {
        "default": -20.0,
        "step": 5.0,
        "min": -35.0,
        "max": -10.0,
        "precision": 0,
    },
    "williams_r__oversold": {
        "default": -80.0,
        "step": 5.0,
        "min": -90.0,
        "max": -65.0,
        "precision": 0,
    },
    "cci_reversion__upper_threshold": {
        "default": 100.0,
        "step": 20.0,
        "min": 100.0,
        "max": 220.0,
        "precision": 0,
    },
    "cci_reversion__lower_threshold": {
        "default": -100.0,
        "step": 20.0,
        "min": -220.0,
        "max": -100.0,
        "precision": 0,
    },
    "stoch_rsi__overbought": {
        "default": 80.0,
        "step": 5.0,
        "min": 70.0,
        "max": 90.0,
        "precision": 0,
    },
    "stoch_rsi__oversold": {
        "default": 20.0,
        "step": 5.0,
        "min": 10.0,
        "max": 30.0,
        "precision": 0,
    },
    "rsi_reversion__intrabar_decay": {
        "default": 0.90,
        "step": 0.04,
        "min": 0.70,
        "max": 0.95,
        "precision": 2,
    },
    "stoch_rsi__intrabar_decay": {
        "default": 0.88,
        "step": 0.04,
        "min": 0.70,
        "max": 0.95,
        "precision": 2,
    },
    "williams_r__intrabar_decay": {
        "default": 0.88,
        "step": 0.04,
        "min": 0.70,
        "max": 0.95,
        "precision": 2,
    },
    "cci_reversion__intrabar_decay": {
        "default": 0.88,
        "step": 0.04,
        "min": 0.70,
        "max": 0.95,
        "precision": 2,
    },
    "bollinger_breakout__intrabar_decay": {
        "default": 0.78,
        "step": 0.04,
        "min": 0.65,
        "max": 0.90,
        "precision": 2,
    },
    "keltner_bb_squeeze__intrabar_decay": {
        "default": 0.80,
        "step": 0.04,
        "min": 0.65,
        "max": 0.90,
        "precision": 2,
    },
    "session_momentum__london_min_atr_pct": {
        "default": 0.00050,
        "step": 0.00006,
        "min": 0.00020,
        "max": 0.00080,
        "precision": 5,
    },
    "session_momentum__other_min_atr_pct": {
        "default": 0.00038,
        "step": 0.00005,
        "min": 0.00015,
        "max": 0.00065,
        "precision": 5,
    },
}


def normalize_strategy_name_list(raw_values: Optional[List[str]]) -> List[str]:
    normalized: List[str] = []
    seen = set()
    for raw in raw_values or []:
        value = str(raw).strip()
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized


def collect_configured_strategies(signal_config: Any) -> List[str]:
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


def resolve_template_strategies(
    signal_config: Any,
    timeframe: str,
    requested: Optional[List[str]] = None,
) -> List[str]:
    requested_list = normalize_strategy_name_list(requested)
    candidates = requested_list or collect_configured_strategies(signal_config)
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


def effective_strategy_params_for_timeframe(
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


def generate_template_values(
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


def build_param_space_template(
    timeframe: str,
    requested_strategies: Optional[List[str]] = None,
) -> Dict[str, Any]:
    signal_config = load_signal_config()
    resolved_strategies = resolve_template_strategies(
        signal_config,
        timeframe,
        requested_strategies,
    )
    effective_strategy_params = effective_strategy_params_for_timeframe(
        signal_config,
        timeframe,
    )

    baseline_strategy_params: Dict[str, float] = {}
    param_space: Dict[str, List[float]] = {}
    for strategy in resolved_strategies:
        strategy_keys = [
            key
            for key in effective_strategy_params.keys()
            if key.startswith(f"{strategy}__")
        ]
        for compound_key in strategy_keys:
            current_value = effective_strategy_params.get(compound_key)
            if current_value is not None:
                baseline_strategy_params[compound_key] = current_value
            values = generate_template_values(compound_key, current_value)
            if values:
                param_space[compound_key] = values

    notes = [
        "param_space 基于 signal.ini 当前生效策略参数生成，可继续在前端编辑。",
        "未显式选择 strategies 时，会按当前 timeframe 自动筛选可运行策略。",
    ]
    if not param_space:
        notes.append(
            "当前所选策略没有已配置的 strategy_params 模板，请手动补充 param_space。"
        )

    return {
        "timeframe": timeframe.upper(),
        "requested_strategies": normalize_strategy_name_list(requested_strategies),
        "resolved_strategies": resolved_strategies,
        "baseline_strategy_params": baseline_strategy_params,
        "param_space": param_space,
        "notes": notes,
    }


def parse_request_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def build_backtest_config(request: BacktestRequestBase) -> Any:
    from src.backtesting.models import BacktestConfig

    defaults = load_backtest_defaults()
    signal_config = load_signal_config()
    config_kwargs: Dict[str, Any] = {
        "symbol": request.symbol,
        "timeframe": request.timeframe,
        "start_time": parse_request_datetime(request.start_time),
        "end_time": parse_request_datetime(request.end_time),
        "strategies": request.strategies,
        "strategy_params": request.strategy_params,
        "strategy_params_per_tf": request.strategy_params_per_tf,
        "regime_affinity_overrides": request.regime_affinity_overrides,
        **strategy_scope_overrides(signal_config),
    }
    for field_name in CONFIG_OVERRIDE_FIELDS:
        value = getattr(request, field_name, None)
        if value is not None:
            config_kwargs[field_name] = value
        elif field_name in defaults:
            config_kwargs[field_name] = defaults[field_name]
    return BacktestConfig.from_flat(**config_kwargs)


def resolve_optimizer_settings(request: BacktestOptimizeRequest) -> Dict[str, Any]:
    defaults = load_backtest_defaults()
    return {
        "search_mode": request.search_mode or defaults.get("search_mode", "grid"),
        "max_combinations": (
            request.max_combinations
            if request.max_combinations is not None
            else defaults.get("max_combinations", 500)
        ),
        "sort_metric": request.sort_metric
        or defaults.get("sort_metric", "sharpe_ratio"),
    }
