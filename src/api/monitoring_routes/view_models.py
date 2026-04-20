from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class HealthLiveView(BaseModel):
    status: str
    timestamp: str


class ReadyProbeView(BaseModel):
    status: str
    checks: Dict[str, str] = Field(default_factory=dict)
    startup_phase: Optional[str] = None
    timestamp: Optional[str] = None


class RuntimeActionView(BaseModel):
    status: str
    message: str


class FailedEventsResetView(RuntimeActionView):
    reset_count: int = 0


class AlertResolutionView(RuntimeActionView):
    pass


class MonitoredComponentsView(BaseModel):
    status: str
    components: List[Dict[str, Any]] = Field(default_factory=list)
    count: int = 0
    check_interval: Optional[float] = None


class TradingTriggerMethodModel(BaseModel):
    id: str
    type: str
    path: str
    description: str


class TradingTriggerMethodsView(BaseModel):
    status: str
    count: int
    methods: List[TradingTriggerMethodModel] = Field(default_factory=list)


class ConfigReloadView(BaseModel):
    success: bool
    reloaded: str
    cache_cleared: bool


class RuntimeTaskStatusItem(BaseModel):
    component: Optional[str] = None
    task_name: Optional[str] = None
    updated_at: Optional[str] = None
    state: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    next_run_at: Optional[str] = None
    duration_ms: Optional[int] = None
    success_count: Optional[int] = None
    failure_count: Optional[int] = None
    consecutive_failures: Optional[int] = None
    last_error: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


class RuntimeTasksView(BaseModel):
    items: List[RuntimeTaskStatusItem] = Field(default_factory=list)
    filters: Dict[str, Optional[str]] = Field(default_factory=dict)


from src.api.schemas import MutationActionResultBase


class PendingEntryCancellationView(MutationActionResultBase):
    cancelled: bool
    signal_id: str


class PendingEntriesBySymbolCancellationView(MutationActionResultBase):
    cancelled_count: int
    symbol: str


class EffectiveRuntimeConfigView(BaseModel):
    model_config = {"extra": "allow"}
