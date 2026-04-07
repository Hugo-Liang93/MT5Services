from __future__ import annotations

from typing import Dict, List, Literal

from pydantic import BaseModel, Field


class TradingConfig(BaseModel):
    symbols: List[str] = Field(default_factory=lambda: ["XAUUSD"])
    timeframes: List[str] = Field(default_factory=lambda: ["M1", "H1"])
    default_symbol: str = "XAUUSD"


class IntervalConfig(BaseModel):
    poll_interval: float = 0.5
    ohlc_interval: float = 30.0
    stream_interval: float = 1.0
    indicator_reload_interval: float = 60.0


class LimitConfig(BaseModel):
    tick_limit: int = 200
    ohlc_limit: int = 200
    tick_cache_size: int = 5000
    ohlc_cache_limit: int = 500
    quote_stale_seconds: float = 1.0


class SystemConfig(BaseModel):
    timezone: str = "UTC"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8808
    runtime_data_dir: str = "data"
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
    stale_after_seconds: float = 1800.0
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
    market_impact_post_windows: List[int] = Field(default_factory=lambda: [5, 15, 30, 60, 120])
    market_impact_final_collection_delay_minutes: int = 130
    market_impact_backfill_enabled: bool = True
    market_impact_backfill_days: int = 30
    market_impact_stats_refresh_interval_seconds: float = 21600.0
    # Trade Guard 动态保护窗口阈值
    market_impact_high_spike_threshold: float = 3.0
    market_impact_high_spike_buffer_minutes: int = 60
    market_impact_med_spike_threshold: float = 2.0
    market_impact_med_spike_buffer_minutes: int = 45


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
    # 交易频率限制（None 或 0 = 不限制）
    max_trades_per_day: int | None = None
    max_trades_per_hour: int | None = None


class TradingOpsConfig(BaseModel):
    dispatch_strict_mode: bool = True
    dispatch_timeout_ms: int = 5000
    daily_summary_recent_limit: int = 1000
    runtime_mode: Literal["full", "observe", "risk_off", "ingest_only"] = "full"
    runtime_mode_after_eod: Literal["disabled", "risk_off", "ingest_only"] = "ingest_only"
    runtime_mode_after_manual_closeout: Literal["disabled", "risk_off", "ingest_only"] = "ingest_only"
    runtime_mode_auto_check_interval_seconds: float = 15.0
    pending_recovery_orphan_action: Literal["record_only", "cancel"] = "record_only"
    pending_recovery_missing_action: Literal["mark_missing", "ignore"] = "mark_missing"
