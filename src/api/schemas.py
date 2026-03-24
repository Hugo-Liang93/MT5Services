"""
Pydantic 模型：用于 API 输入/输出的统一定义。
AI友好接口优化 - 扩展ApiResponse模型
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, TypeVar, Generic, List, Dict, Any

from pydantic import BaseModel, Field, model_validator


class QuoteModel(BaseModel):
    symbol: str
    bid: float
    ask: float
    last: float
    volume: float
    time: str


class TickModel(BaseModel):
    symbol: str
    price: float
    volume: float
    time: str
    time_msc: Optional[int] = None


class OHLCModel(BaseModel):
    symbol: str
    timeframe: str
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    indicators: Optional[Dict[str, Any]] = None


class AccountInfoModel(BaseModel):
    login: int
    balance: float
    equity: float
    margin: float
    margin_free: float
    leverage: int
    currency: str


class PositionModel(BaseModel):
    ticket: int
    symbol: str
    volume: float
    price_open: float
    sl: float
    tp: float
    time: str
    type: int
    magic: int
    comment: str


class OrderModel(BaseModel):
    ticket: int
    symbol: str
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    time: str
    type: int
    magic: int
    comment: str


class TradeRequest(BaseModel):
    symbol: str
    volume: float
    side: Optional[str] = None
    direction: Optional[str] = None  # alias for side
    order_kind: str = "market"
    price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    deviation: int = 20
    comment: str = ""
    magic: int = 0
    dry_run: bool = False
    request_id: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _resolve_side(cls, values: Any) -> Any:
        if isinstance(values, dict):
            if not values.get("side") and values.get("direction"):
                values["side"] = values["direction"]
        return values


class TradePrecheckModel(BaseModel):
    enabled: bool
    mode: str
    event_blocked: bool = False
    calendar_health_degraded: bool = False
    blocked: bool
    verdict: str
    reason: Optional[str] = None
    symbol: str
    active_windows: List[EconomicCalendarMergedRiskWindowModel] = Field(default_factory=list)
    upcoming_windows: List[EconomicCalendarMergedRiskWindowModel] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    calendar_health_mode: str = "warn_only"
    calendar_health: Dict[str, Any] = Field(default_factory=dict)
    checks: List[Dict[str, Any]] = Field(default_factory=list)
    estimated_margin: Optional[float] = None
    margin_error: Optional[str] = None
    intent: Dict[str, Any] = Field(default_factory=dict)


class CloseRequest(BaseModel):
    ticket: int
    volume: Optional[float] = None
    deviation: int = 20
    comment: str = ""


class CloseAllRequest(BaseModel):
    symbol: Optional[str] = None
    magic: Optional[int] = None
    side: Optional[str] = None
    deviation: int = 20
    comment: str = "close_all"


class TradeControlRequest(BaseModel):
    auto_entry_enabled: Optional[bool] = None
    close_only_mode: Optional[bool] = None
    reason: str = ""
    reset_circuit: bool = False


class TradeReconcileRequest(BaseModel):
    sync_open_positions: bool = True

class CancelOrdersRequest(BaseModel):
    symbol: Optional[str] = None
    magic: Optional[int] = None


class BatchTradeRequest(BaseModel):
    trades: List[TradeRequest] = Field(default_factory=list)
    stop_on_error: bool = False


class BatchCloseRequest(BaseModel):
    tickets: List[int] = Field(default_factory=list)
    deviation: int = 20
    comment: str = ""


class BatchCancelOrdersRequest(BaseModel):
    tickets: List[int] = Field(default_factory=list)


class TradeDispatchRequest(BaseModel):
    operation: str
    payload: Dict[str, Any] = Field(default_factory=dict)

class EstimateMarginRequest(BaseModel):
    symbol: str
    volume: float
    side: Optional[str] = None
    direction: Optional[str] = None  # alias for side
    price: Optional[float] = None

    @model_validator(mode="before")
    @classmethod
    def _resolve_side(cls, values: Any) -> Any:
        if isinstance(values, dict):
            if not values.get("side") and values.get("direction"):
                values["side"] = values["direction"]
        return values


class ModifyOrdersRequest(BaseModel):
    symbol: Optional[str] = None
    magic: Optional[int] = None
    sl: Optional[float] = None
    tp: Optional[float] = None


class ModifyPositionsRequest(BaseModel):
    symbol: Optional[str] = None
    magic: Optional[int] = None
    sl: Optional[float] = None
    tp: Optional[float] = None


class SymbolInfoModel(BaseModel):
    symbol: str
    description: str
    digits: int
    point: float
    trade_contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    margin_initial: float
    margin_maintenance: float
    tick_value: float
    tick_size: float


class TradingAccountModel(BaseModel):
    alias: str
    label: str
    login: Optional[int] = None
    server: Optional[str] = None
    timezone: str
    enabled: bool = True
    default: bool = False
    active: bool = False


class EconomicCalendarEventModel(BaseModel):
    scheduled_at: str
    scheduled_at_local: Optional[str] = None
    local_timezone: Optional[str] = None
    scheduled_at_release: Optional[str] = None
    release_timezone: Optional[str] = None
    event_uid: str
    source: str
    provider_event_id: str
    event_name: str
    country: Optional[str] = None
    category: Optional[str] = None
    currency: Optional[str] = None
    reference: Optional[str] = None
    actual: Optional[str] = None
    previous: Optional[str] = None
    forecast: Optional[str] = None
    revised: Optional[str] = None
    importance: Optional[int] = None
    unit: Optional[str] = None
    release_id: Optional[str] = None
    source_url: Optional[str] = None
    all_day: bool = False
    session_bucket: str = "off_hours"
    is_asia_session: bool = False
    is_europe_session: bool = False
    is_us_session: bool = False
    status: str = "scheduled"
    first_seen_at: str
    last_seen_at: str
    released_at: Optional[str] = None
    last_value_check_at: Optional[str] = None
    ingested_at: str
    last_updated: str


class EconomicCalendarRefreshModel(BaseModel):
    job_type: Optional[str] = None
    status: str
    fetched: int
    written: int
    snapshots_written: int = 0
    deleted: int = 0
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration_ms: Optional[str] = None
    provider_counts: Optional[str] = None
    provider_errors: Optional[str] = None


class EconomicCalendarStatusModel(BaseModel):
    enabled: str
    running: str
    local_timezone: Optional[str] = None
    refresh_interval_seconds: Optional[str] = None
    last_refresh_at: Optional[str] = None
    last_refresh_started_at: Optional[str] = None
    last_refresh_completed_at: Optional[str] = None
    last_refresh_error: Optional[str] = None
    last_refresh_status: Optional[str] = None
    refresh_in_progress: Optional[str] = None
    last_refresh_duration_ms: Optional[str] = None
    consecutive_failures: Optional[str] = None
    stale: Optional[str] = None
    default_countries: Optional[str] = None
    provider_status: Optional[Dict[str, Any]] = None
    calendar_sync_interval_seconds: Optional[str] = None
    near_term_refresh_interval_seconds: Optional[str] = None
    release_watch_interval_seconds: Optional[str] = None
    near_term_window_hours: Optional[str] = None
    release_watch_lookback_minutes: Optional[str] = None
    release_watch_lookahead_minutes: Optional[str] = None
    job_status: Optional[Dict[str, Any]] = None


class EconomicCalendarRiskWindowModel(BaseModel):
    event_uid: str
    event_name: str
    source: str
    country: Optional[str] = None
    currency: Optional[str] = None
    importance: Optional[int] = None
    session_bucket: str
    window_start: str
    window_end: str
    scheduled_at: str
    scheduled_at_local: Optional[str] = None
    scheduled_at_release: Optional[str] = None


class EconomicCalendarMergedRiskWindowModel(BaseModel):
    window_start: str
    window_end: str
    event_count: int
    event_uids: List[str]
    event_names: List[str]
    sources: List[str]
    countries: List[str]
    currencies: List[str]
    sessions: List[str]
    max_importance: Optional[int] = None


class EconomicCalendarTradeGuardModel(BaseModel):
    symbol: str
    evaluation_time: str
    blocked: bool
    currencies: List[str]
    countries: List[str]
    active_windows: List[EconomicCalendarMergedRiskWindowModel]
    upcoming_windows: List[EconomicCalendarMergedRiskWindowModel]
    importance_min: int


class EconomicCalendarUpdateModel(BaseModel):
    recorded_at: str
    event_uid: str
    scheduled_at: str
    source: str
    provider_event_id: str
    event_name: str
    country: Optional[str] = None
    currency: Optional[str] = None
    status: str
    snapshot_reason: str
    job_type: str
    actual: Optional[str] = None
    previous: Optional[str] = None
    forecast: Optional[str] = None
    revised: Optional[str] = None
    importance: Optional[int] = None
    raw_payload: Dict[str, Any] = Field(default_factory=dict)


class RuntimeTaskStatusModel(BaseModel):
    component: str
    task_name: str
    updated_at: str
    state: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    next_run_at: Optional[str] = None
    duration_ms: Optional[int] = None
    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """AI友好的API响应格式
    
    扩展原有ApiResponse，添加错误信息和元数据字段
    便于AI agent解析和处理
    """
    success: bool = True
    data: Optional[T] = None
    error: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    
    @classmethod
    def success_response(cls, data: T, metadata: Optional[Dict[str, Any]] = None) -> "ApiResponse[T]":
        """创建成功响应
        
        Args:
            data: 响应数据
            metadata: 元数据，可包含时间戳、数据来源等信息
            
        Returns:
            ApiResponse实例
        """
        default_metadata = {
            "timestamp": datetime.now().isoformat(),
            "data_source": "mt5_realtime",
            "data_freshness": "fresh"
        }
        if metadata:
            default_metadata.update(metadata)
        
        return cls(
            success=True,
            data=data,
            error=None,
            metadata=default_metadata
        )
    
    @classmethod
    def error_response(cls, 
                      error_code: str, 
                      error_message: str, 
                      suggested_action: Optional[str] = None,
                      details: Optional[Dict[str, Any]] = None) -> "ApiResponse[None]":
        """创建错误响应
        
        Args:
            error_code: 错误代码，AI可识别的标识
            error_message: 错误描述，人类可读
            suggested_action: 建议AI执行的动作
            details: 错误详情，用于调试
            
        Returns:
            ApiResponse实例
        """
        normalized_error_code = str(getattr(error_code, "value", error_code)).lower()
        normalized_action = getattr(suggested_action, "value", suggested_action)
        return cls(
            success=False,
            data=None,
            error={
                "code": normalized_error_code,
                "message": error_message,
                "suggested_action": normalized_action,
                "details": details or {}
            },
            metadata={
                "timestamp": datetime.now().isoformat(),
                "data_source": "error"
            }
        )


class SignalEvaluateRequest(BaseModel):
    symbol: str
    timeframe: str
    strategy: Optional[str] = None  # 为空时评估所有策略并返回最优结果
    indicators: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SignalDecisionModel(BaseModel):
    strategy: str
    symbol: str
    timeframe: str
    direction: str
    confidence: float
    reason: str
    used_indicators: List[str] = Field(default_factory=list)
    timestamp: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SignalEventModel(BaseModel):
    generated_at: Optional[str] = None
    signal_id: str
    symbol: str
    timeframe: str
    strategy: str
    direction: str
    confidence: float
    reason: str
    scope: str = "confirmed"
    used_indicators: List[str] = Field(default_factory=list)
    indicators_snapshot: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SignalSummaryModel(BaseModel):
    symbol: str
    timeframe: str
    strategy: str
    direction: str
    count: int
    scope: str = "confirmed"
    avg_confidence: Optional[float] = None
    last_seen_at: Optional[str] = None


class SignalExecuteTradeRequest(BaseModel):
    """AI agent dispatch: execute a trade derived from a confirmed signal.

    The system looks up the signal by signal_id, computes ATR-based SL/TP
    from the stored indicator snapshot, then submits the order via TradingModule.

    dry_run=True 时仅返回计算出的交易参数而不实际下单，方便测试和调试。
    """

    signal_id: str
    account_alias: Optional[str] = None
    volume_override: Optional[float] = Field(default=None, gt=0, le=100.0)
    dry_run: bool = False
