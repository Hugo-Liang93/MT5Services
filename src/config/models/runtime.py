from __future__ import annotations

from typing import Dict, List, Literal

from pydantic import BaseModel, Field, model_validator


class TradingConfig(BaseModel):
    symbols: List[str] = Field(default_factory=lambda: ["XAUUSD"])
    timeframes: List[str] = Field(default_factory=lambda: ["M1", "H1"])
    default_symbol: str = "XAUUSD"


class IntervalConfig(BaseModel):
    poll_interval: float = 0.5
    ohlc_interval: float = 30.0
    ohlc_intervals: Dict[str, float] = Field(default_factory=dict)
    stream_interval: float = 1.0
    indicator_reload_interval: float = 60.0


class LimitConfig(BaseModel):
    tick_limit: int = 200
    ohlc_limit: int = 200
    tick_cache_size: int = 5000
    ohlc_cache_limit: int = 500
    quote_stale_seconds: float = 1.0


class TickFeatureRuntimeConfig(BaseModel):
    window_seconds: float = 5.0
    emit_interval_seconds: float = 1.0
    max_quote_age_ms: int = 1500
    max_spread_points: float = 30.0
    min_ticks_per_window: int = 3
    point_size: float = 0.00001
    max_snapshot_age_seconds: float = 3.0
    bus_maxlen: int = 4096
    point_size_by_symbol: Dict[str, float] = Field(default_factory=dict)
    max_spread_points_by_symbol: Dict[str, float] = Field(default_factory=dict)


class SystemConfig(BaseModel):
    timezone: str = "UTC"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8808
    runtime_data_dir: str = "data/runtime"
    modules_enabled: List[str] = Field(
        default_factory=lambda: ["ingest", "api", "indicators", "storage"]
    )
    # 日志文件持久化
    log_file_enabled: bool = True
    log_dir: str = "data/logs"
    log_file_max_mb: int = 100
    log_file_backup_count: int = 10
    log_error_file: str = "errors.log"


class APIConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8808
    enable_cors: bool = True
    docs_enabled: bool = True
    redoc_enabled: bool = True
    auth_enabled: bool = False
    api_key_header: str = "X-API-Key"
    api_key: str | None = None
    access_log_enabled: bool = True
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class IngestConfig(BaseModel):
    tick_initial_lookback_seconds: int = 20
    ohlc_backfill_limit: int = 500
    retry_attempts: int = 3
    retry_backoff: float = 1.0
    connection_timeout: float = 10.0
    max_concurrent_symbols: int = 5
    queue_monitor_interval: float = 5.0
    health_check_interval: float = 30.0
    max_allowed_delay: float = 60.0
    intrabar_interval: float = 15.0
    intrabar_intervals: Dict[str, float] = Field(default_factory=dict)
    # error_recovery: 连续失败退避参数
    symbol_error_threshold: int = 5
    symbol_cooldown_seconds: float = 60.0
    symbol_max_cooldown_seconds: float = 300.0
    intrabar_drop_rate_1m_warning: float = 1.0
    intrabar_drop_rate_1m_critical: float = 5.0
    intrabar_queue_age_p95_ms_warning: float = 2500.0
    intrabar_queue_age_p95_ms_critical: float = 5000.0
    intrabar_to_decision_latency_p95_ms_warning: float = 3500.0
    intrabar_to_decision_latency_p95_ms_critical: float = 7000.0


class EconomicConfig(BaseModel):
    enabled: bool = True
    lookback_days: int = 7
    lookahead_days: int = 14
    request_timeout_seconds: float = 10.0
    local_timezone: str = "UTC"
    refresh_interval_seconds: float = 900.0
    calendar_sync_interval_seconds: float = 21600.0
    near_term_refresh_interval_seconds: float = 900.0
    release_watch_interval_seconds: float = 60.0
    startup_calendar_sync_delay_seconds: float = 180.0
    refresh_jitter_seconds: float = 5.0
    startup_refresh: bool = True
    request_retries: int = 3
    retry_backoff_seconds: float = 1.0
    # P9 bug #7: 1800→2400，避免 jin10 抖动贴边误报（详见 economic.ini 注释）
    stale_after_seconds: float = 2400.0
    high_importance_threshold: int = 3
    pre_event_buffer_minutes: int = 30
    post_event_buffer_minutes: int = 30
    near_term_window_hours: int = 72
    release_watch_lookback_minutes: int = 15
    release_watch_lookahead_minutes: int = 120
    # 三档自适应 release_watch
    release_watch_idle_interval_seconds: float = 1800.0
    release_watch_approaching_interval_seconds: float = 600.0
    release_watch_active_interval_seconds: float = 120.0
    release_watch_approaching_minutes: int = 120
    release_watch_active_minutes: int = 30
    release_watch_post_event_minutes: int = 15
    release_watch_importance_min: int = 2
    default_countries: List[str] = Field(default_factory=list)
    fred_release_whitelist_ids: List[str] = Field(default_factory=list)
    fred_release_whitelist_keywords: List[str] = Field(default_factory=list)
    fred_release_blacklist_keywords: List[str] = Field(default_factory=list)
    # Per-job provider 路由（逗号分隔，为空则使用全部已配置 provider）
    calendar_sync_sources: List[str] = Field(default_factory=list)
    near_term_sync_sources: List[str] = Field(default_factory=list)
    release_watch_sources: List[str] = Field(default_factory=list)
    curated_sources: List[str] = Field(default_factory=lambda: ["jin10"])
    curated_countries: List[str] = Field(default_factory=list)
    curated_currencies: List[str] = Field(default_factory=list)
    curated_statuses: List[str] = Field(
        default_factory=lambda: ["scheduled", "imminent", "pending_release", "released"]
    )
    curated_importance_min: int | None = 2
    curated_include_all_day: bool = False
    trade_guard_enabled: bool = True
    trade_guard_mode: str = "block"
    trade_guard_calendar_health_mode: str = "warn_only"
    trade_guard_lookahead_minutes: int = 180
    trade_guard_lookback_minutes: int = 0
    trade_guard_importance_min: int | None = None
    trade_guard_provider_failure_threshold: int = 3
    # 分级压制：低于 block_importance_min 的事件仅 warn，不 block
    trade_guard_block_importance_min: int = 3
    trade_guard_warn_pre_buffer_minutes: int = 15
    trade_guard_warn_post_buffer_minutes: int = 10
    # 品种相关性过滤：非直接相关事件 importance 自动降 1 级
    trade_guard_relevance_filter_enabled: bool = False
    gold_impact_keywords: str = ""
    # DB 事件 category 字段白名单（逗号分隔）。事件 category 命中任一值 →
    # 直接视为与黄金相关（无需关键词匹配，大小写不敏感）。用于补齐关键词
    # 易漏的长尾事件类型（如"Central Bank Speech"/"Monetary Policy"）。
    gold_impact_categories: str = ""
    tradingeconomics_enabled: bool = True
    tradingeconomics_api_key: str | None = None
    fred_enabled: bool = True
    fred_api_key: str | None = None
    # FMP (Financial Modeling Prep)
    fmp_enabled: bool = False
    fmp_api_key: str | None = None
    # Jin10 (金十数据)
    jin10_enabled: bool = True
    jin10_token: str | None = None
    # Alpha Vantage
    alphavantage_enabled: bool = False
    alphavantage_api_key: str | None = None
    alphavantage_tracked_indicators: List[str] = Field(default_factory=list)
    # Market Impact 行情影响统计
    market_impact_enabled: bool = False
    market_impact_symbols: List[str] = Field(default_factory=lambda: ["XAUUSD"])
    market_impact_timeframes: List[str] = Field(default_factory=lambda: ["M1", "M5"])
    market_impact_importance_min: int = 2
    market_impact_pre_windows: List[int] = Field(default_factory=lambda: [30, 60, 120])
    market_impact_post_windows: List[int] = Field(
        default_factory=lambda: [5, 15, 30, 60, 120]
    )
    market_impact_final_collection_delay_minutes: int = 130
    market_impact_backfill_enabled: bool = True
    market_impact_backfill_days: int = 30
    market_impact_stats_refresh_interval_seconds: float = 21600.0
    # Trade Guard 动态保护窗口阈值
    market_impact_high_spike_threshold: float = 3.0
    market_impact_high_spike_buffer_minutes: int = 60
    market_impact_med_spike_threshold: float = 2.0
    market_impact_med_spike_buffer_minutes: int = 45


class PreflightRiskPolicy(BaseModel):
    live_max_positions_per_symbol: int = 3
    live_max_volume_per_order: float = 0.03
    live_max_volume_per_symbol: float = 0.03
    live_max_daily_loss_limit_pct: float = 5.0


class RecoveryExecutionCanaryConfig(BaseModel):
    enabled: bool = False
    dry_run: bool = True
    order_kind: Literal["market", "limit", "stop"] = "market"
    deviation: int = 20
    magic: int = 0
    comment_prefix: str = "recovery"
    protective_stop_points: float | None = None


class RecoveryRuntimeRunnerConfig(BaseModel):
    enabled: bool = False
    dry_run: bool = True
    demo_only: bool = True
    symbol: str = "XAUUSD"
    direction: Literal["buy", "sell"] = "buy"
    strategy: str = "tick_martingale_probe"
    timeframe: str = "TICK"
    base_volume: float = 0.01
    multiplier: float = 2.0
    max_steps: int = 1
    max_total_volume: float = 0.03
    max_next_volume: float | None = 0.02
    step_distance_points: float = 80.0
    max_step_adverse_move_points: float = 0.0
    recovery_target_points: float = 5.0
    point: float = 0.01
    min_step_interval_ms: int = 0
    volume_step: float = 0.01
    contract_size: float = 100.0
    direction_mode: Literal["fixed", "auto"] = "fixed"
    min_directional_move_points: float = 0.0
    max_directional_move_points: float = 0.0
    min_pressure_delta: float = 0.0
    max_entry_spread_points: float | None = None
    slippage_budget_points: float = 0.0
    commission_points: float = 0.0
    min_net_profit_points: float = 0.0
    max_cycle_loss_points: float = 0.0
    max_cycle_duration_seconds: float = 0.0
    max_steps_exit_mode: Literal["hold", "close_cycle"] = "hold"
    max_steps_hold_seconds: float = 0.0
    entry_confirmation_snapshots: int = 1
    entry_confirmation_max_gap_seconds: float = 3.0
    order_kind: Literal["market", "limit", "stop"] = "market"
    deviation: int = 20
    magic: int = 0
    comment_prefix: str = "recovery-runner"
    protective_stop_points: float | None = 80.0
    max_cycles_per_session: int = 1
    max_cycles_per_day: int = 0
    min_cycle_interval_seconds: float = 0.0
    cooldown_after_cycle_close_seconds: float = 0.0
    max_cycles_per_hour: int = 0
    snapshot_stale_seconds: float = 5.0
    blocked_dispatch_retry_seconds: float = 30.0
    real_trade_calibration_guard_enabled: bool = True
    real_trade_calibration_min_samples: int = 50
    real_trade_calibration_max_target_shortfall_p90_points: float = 0.0
    real_trade_calibration_min_net_margin_p50_points: float = 0.0
    risk_profile: str = "recovery_budgeted"
    max_daily_recovery_loss_amount: float = 0.0
    max_rolling_recovery_loss_amount: float = 0.0
    rolling_loss_window_minutes: int = 60
    max_consecutive_loss_cycles: int = 0
    loss_lockout_minutes: int = 0


RiskPreTradeRuleName = Literal[
    "account_snapshot",
    "daily_loss_limit",
    "margin_availability",
    "trade_frequency",
    "protection",
    "session_window",
    "market_structure",
    "economic_event",
    "calendar_health",
]


def _standard_kline_pre_trade_rules() -> List[RiskPreTradeRuleName]:
    return [
        "account_snapshot",
        "daily_loss_limit",
        "margin_availability",
        "trade_frequency",
        "protection",
        "session_window",
        "market_structure",
        "economic_event",
        "calendar_health",
    ]


def _recovery_budgeted_pre_trade_rules() -> List[RiskPreTradeRuleName]:
    return [
        "account_snapshot",
        "daily_loss_limit",
        "margin_availability",
        "protection",
        "session_window",
        "economic_event",
        "calendar_health",
    ]


class RiskProfileConfig(BaseModel):
    policy: Literal["standard_kline", "recovery_budgeted"] = "standard_kline"
    trade_frequency_enabled: bool = True
    pre_trade_rules: List[RiskPreTradeRuleName] = Field(
        default_factory=_standard_kline_pre_trade_rules
    )
    max_daily_recovery_loss_amount: float | None = None
    max_rolling_recovery_loss_amount: float | None = None
    rolling_loss_window_minutes: int = 60
    max_consecutive_loss_cycles: int = 0
    loss_lockout_minutes: int = 0

    @model_validator(mode="before")
    @classmethod
    def _apply_policy_rule_defaults(cls, data):
        if not isinstance(data, dict):
            return data
        if "pre_trade_rules" in data:
            return data
        policy = str(data.get("policy") or "standard_kline").strip()
        payload = dict(data)
        if policy == "recovery_budgeted":
            payload["pre_trade_rules"] = _recovery_budgeted_pre_trade_rules()
        else:
            payload["pre_trade_rules"] = _standard_kline_pre_trade_rules()
        return payload

    @model_validator(mode="after")
    def _validate_trade_frequency_flag(self):
        enabled_by_rules = "trade_frequency" in set(self.pre_trade_rules)
        if bool(self.trade_frequency_enabled) != enabled_by_rules:
            raise ValueError(
                "trade_frequency_enabled must match pre_trade_rules"
            )
        return self


def _default_risk_profiles() -> Dict[str, RiskProfileConfig]:
    return {
        "standard_kline": RiskProfileConfig(
            policy="standard_kline",
            trade_frequency_enabled=True,
            pre_trade_rules=_standard_kline_pre_trade_rules(),
        ),
        "recovery_budgeted": RiskProfileConfig(
            policy="recovery_budgeted",
            trade_frequency_enabled=False,
            pre_trade_rules=_recovery_budgeted_pre_trade_rules(),
        ),
    }


class RiskConfig(BaseModel):
    enabled: bool = True
    allowed_sessions: str = ""
    max_positions_per_symbol: int | None = None
    max_open_positions_total: int | None = None
    max_pending_orders_per_symbol: int | None = None
    max_volume_per_order: float | None = None
    max_volume_per_symbol: float | None = None
    max_net_lots_per_symbol: float | None = None
    daily_loss_limit_pct: float | None = None
    # 市价单保护模式：off / sl / sl_or_tp
    market_order_protection: Literal["off", "sl", "sl_or_tp"] = "sl"
    # 保证金安全系数（1.2 = 要求可用保证金 >= 预估保证金 × 1.2）
    margin_safety_factor: float | None = 1.2
    # 风控事实源不可用时的策略：warn_only=记录告警后放行；fail_closed=拒绝新单。
    data_unavailable_policy: Literal["warn_only", "fail_closed"] = "warn_only"
    preflight_policy: PreflightRiskPolicy = Field(default_factory=PreflightRiskPolicy)
    recovery_execution_canary: RecoveryExecutionCanaryConfig = Field(
        default_factory=RecoveryExecutionCanaryConfig
    )
    recovery_runtime_runner: RecoveryRuntimeRunnerConfig = Field(
        default_factory=RecoveryRuntimeRunnerConfig
    )
    risk_profiles: Dict[str, RiskProfileConfig] = Field(
        default_factory=_default_risk_profiles
    )
    risk_profile_bindings: Dict[str, str] = Field(default_factory=dict)
    # 交易频率限制（None 或 0 = 不限制）
    max_trades_per_day: int | None = None
    max_trades_per_hour: int | None = None


class TradingOpsConfig(BaseModel):
    dispatch_strict_mode: bool = True
    dispatch_timeout_ms: int = 5000
    daily_summary_recent_limit: int = 1000
    runtime_mode: Literal["full", "observe", "risk_off", "ingest_only"] = "full"
    runtime_mode_after_eod: Literal["disabled", "risk_off", "ingest_only"] = (
        "ingest_only"
    )
    runtime_mode_after_manual_closeout: Literal[
        "disabled", "risk_off", "ingest_only"
    ] = "ingest_only"
    runtime_mode_auto_check_interval_seconds: float = 15.0
    pending_recovery_orphan_action: Literal["record_only", "cancel"] = "record_only"
    pending_recovery_missing_action: Literal["mark_missing", "ignore"] = "mark_missing"
