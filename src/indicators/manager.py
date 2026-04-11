"""
Unified indicator manager.

Event-driven indicator computation backed by the market data cache.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections import OrderedDict
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from src.config import get_runtime_data_path
from src.config.indicator_config import (
    UnifiedIndicatorConfig,
    get_global_config_manager,
)
from src.market import MarketDataService
from src.utils.event_store import get_event_store
from .engine.dependency_manager import get_global_dependency_manager
from .engine.pipeline import get_global_pipeline
from .monitoring.metrics_collector import get_global_collector
from .runtime import lifecycle as _lifecycle
from .runtime.manager_bindings import QueryBindingMixin, RegistryBindingMixin

if TYPE_CHECKING:
    from src.persistence.storage_writer import StorageWriter

logger = logging.getLogger(__name__)


@dataclass
class IndicatorResult:
    name: str
    value: dict[str, Any]
    symbol: str
    timeframe: str
    timestamp: datetime
    bar_time: datetime | None = None
    cache_hit: bool = False
    incremental: bool = False
    compute_time_ms: float = 0.0
    success: bool = True
    error: str | None = None


@dataclass
class IndicatorSnapshot:
    symbol: str
    timeframe: str
    data: dict[str, Any]
    timestamp: datetime
    bar_time: datetime | None = None
    compute_time_ms: float = 0.0


@dataclass
class IndicatorRuntimeState:
    """Single container for UnifiedIndicatorManager runtime state."""

    indicator_funcs: dict[str, Callable]
    results: OrderedDict[str, IndicatorResult]
    results_lock: threading.RLock
    results_max: int
    pipeline_event_bus: Any | None
    stop_event: threading.Event
    event_thread: threading.Thread | None
    reload_thread: threading.Thread | None
    last_reconcile_at: datetime | None
    snapshot_listeners: list[
        Callable[[str, str, datetime, dict[str, dict[str, float]], str], None]
    ]
    snapshot_listeners_lock: threading.Lock
    last_preview_snapshot: OrderedDict[
        str, tuple[datetime, dict[str, dict[str, float]]]
    ]
    preview_snapshot_max_entries: int
    priority_indicator_groups: tuple[tuple[str, ...], ...]
    last_intrabar_compute: dict[str, float]
    intrabar_queue: queue.Queue
    intrabar_thread: threading.Thread | None
    intrabar_no_listener_skips: int
    intrabar_no_listener_last_log_at: float
    intrabar_eligible_cache: frozenset | None
    event_write_queue: queue.Queue
    writer_thread: threading.Thread | None
    scope_stats: dict[str, dict[str, int]]
    scope_stats_lock: threading.Lock
    intrabar_metrics_lock: threading.Lock
    intrabar_dropped_count: int
    intrabar_queue_age_samples_ms: deque[float]
    intrabar_process_latency_samples_ms: deque[float]
    confirmed_eligible_cache: frozenset | None = None
    closed_bar_event_sink: Callable[..., None] | None = None
    intrabar_listener: Callable[..., None] | None = None


class UnifiedIndicatorManager(RegistryBindingMixin, QueryBindingMixin):
    """
    Single runtime entrypoint for indicator registration, scheduling and persistence.

    Runtime behavior:
    - consume closed-bar events from the durable event store
- reconcile all configured symbol/timeframe pairs on an interval as a recovery path to cover computation gaps
    - write indicator values back into the market cache and the OHLC persistence channel
    """

    def __init__(
        self,
        market_service: MarketDataService,
        config: UnifiedIndicatorConfig | None = None,
        config_file: str | None = None,
        storage_writer: "StorageWriter" | None = None,
    ):
        self.market_service = market_service
        self.storage_writer = storage_writer
        self.config_manager = get_global_config_manager(config_file)
        self.config = config or self.config_manager.get_config()
        self.event_store = get_event_store(get_runtime_data_path("events.db"))

        # Unified runtime state port: explicit state ownership and lifecycle data.
        self.state = IndicatorRuntimeState(
            indicator_funcs={},
            results=OrderedDict(),
            results_lock=threading.RLock(),
            results_max=2000,
            pipeline_event_bus=None,
            stop_event=threading.Event(),
            event_thread=None,
            reload_thread=None,
            last_reconcile_at=None,
            snapshot_listeners=[],
            snapshot_listeners_lock=threading.Lock(),
            last_preview_snapshot=OrderedDict(),
            preview_snapshot_max_entries=500,
            priority_indicator_groups=(),
            # Throttle guard: minimum wall-clock gap between intrabar computations
            # per (symbol, timeframe). The ingestor trigger mode already
            # controls frequency via child TF bar close events; this is a
            # defense-in-depth guard that prevents duplicate runs caused by race
            # conditions.
            last_intrabar_compute={},
            intrabar_queue=queue.Queue(maxsize=1024),
            intrabar_thread=None,
            intrabar_no_listener_skips=0,
            intrabar_no_listener_last_log_at=0.0,
            # Cached set of indicator names eligible for intrabar snapshots.
            # Built once in _register_indicators() and invalidated on _reinitialize().
            intrabar_eligible_cache=None,
            event_write_queue=queue.Queue(maxsize=2048),
            writer_thread=None,
            scope_stats={
                "confirmed": {"computations": 0, "indicators": 0},
                "intrabar": {"computations": 0, "indicators": 0},
                "reconcile": {"computations": 0, "indicators": 0},
            },
            scope_stats_lock=threading.Lock(),
            intrabar_metrics_lock=threading.Lock(),
            intrabar_dropped_count=0,
            intrabar_queue_age_samples_ms=deque(maxlen=1024),
            intrabar_process_latency_samples_ms=deque(maxlen=1024),
            closed_bar_event_sink=None,
            intrabar_listener=None,
        )

        # Pipeline tracing（由装配层注入，统一通过显式端口访问）
        self.current_trace_id: str | None = None

        self._init_components()
        self._register_indicators()

        logger.info(
            "UnifiedIndicatorManager initialized with %s indicators across %s symbols x %s timeframes",
            len(self.config.indicators),
            len(self.config.symbols),
            len(self.config.timeframes),
        )

    def _any_thread_alive(self) -> bool:
        """任一后台线程存活则返回 True。"""
        return _lifecycle.any_thread_alive(self)

    def set_pipeline_event_bus(self, bus: Any | None) -> None:
        """For app assembly layer: inject pipeline tracing bus via public port."""
        self.state.pipeline_event_bus = bus

    @property
    def pipeline_event_bus(self) -> Any | None:
        return self.state.pipeline_event_bus

    @pipeline_event_bus.setter
    def pipeline_event_bus(self, bus: Any | None) -> None:
        self.state.pipeline_event_bus = bus

    def get_pipeline_event_bus(self) -> Any | None:
        """Expose pipeline tracing bus via public query port."""
        return self.pipeline_event_bus

    def get_config(self) -> UnifiedIndicatorConfig:
        """Expose runtime configuration via public port."""
        config = vars(self).get("config")
        if config is None:
            config = get_global_config_manager().get_config()
            self.config = config
        return config

    def get_current_trace_id(self) -> str | None:
        return self.current_trace_id

    def set_current_trace_id(self, trace_id: str | None) -> None:
        self.current_trace_id = trace_id

    def get_current_spread(self, symbol: str) -> float:
        return self.market_service.get_current_spread(symbol)

    def get_symbol_point(self, symbol: str) -> float | None:
        return self.market_service.get_symbol_point(symbol)

    def start(self) -> None:
        _lifecycle.start(self)

    def stop(self) -> None:
        _lifecycle.stop(self)

    def is_running(self) -> bool:
        return _lifecycle.is_running(self)

    def shutdown(self) -> None:
        self.stop()
        self.pipeline.shutdown()


_global_unified_manager: UnifiedIndicatorManager | None = None


def get_global_unified_manager(
    market_service: MarketDataService | None = None,
    config_file: str | None = None,
    storage_writer: "StorageWriter" | None = None,
    start_immediately: bool | None = None,
) -> UnifiedIndicatorManager:
    global _global_unified_manager
    if _global_unified_manager is None:
        if market_service is None:
            raise ValueError(
                "market_service is required on the first manager initialization"
            )
        _global_unified_manager = UnifiedIndicatorManager(
            market_service=market_service,
            config_file=config_file,
            storage_writer=storage_writer,
        )
        should_start = (
            _global_unified_manager.config.auto_start
            if start_immediately is None
            else start_immediately
        )
        if should_start:
            _global_unified_manager.start()
    return _global_unified_manager


def shutdown_global_unified_manager() -> None:
    global _global_unified_manager
    if _global_unified_manager is not None:
        _global_unified_manager.shutdown()
        _global_unified_manager = None
