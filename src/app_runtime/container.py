"""AppContainer — pure data holder for all runtime components."""

from __future__ import annotations

from typing import Any, Callable, Optional

from src.calendar import EconomicCalendarService
from src.indicators.manager import UnifiedIndicatorManager
from src.ingestion.ingestor import BackgroundIngestor
from src.market import MarketDataService
from src.market_structure import MarketStructureAnalyzer
from src.persistence.storage_writer import StorageWriter
from src.signals.evaluation.calibrator import ConfidenceCalibrator
from src.signals.evaluation.performance import StrategyPerformanceTracker
from src.signals.orchestration import SignalRuntime
from src.signals.service import SignalModule
from src.signals.strategies.htf_cache import HTFStateCache
from src.trading import TradingAccountRegistry, TradingModule
from src.trading.pending_entry import PendingEntryManager
from src.trading.position_manager import PositionManager
from src.trading.signal_executor import TradeExecutor
from src.trading.signal_quality_tracker import SignalQualityTracker
from src.monitoring.pipeline_event_bus import PipelineEventBus
from src.readmodels.runtime import RuntimeReadModel
from src.trading.trade_outcome_tracker import TradeOutcomeTracker
from src.studio.service import StudioService


class AppContainer:
    """Flat component holder — no logic, no property proxies.

    Each field is ``None`` until populated by :func:`build_app_container`.
    Multiple instances can coexist (backtest, test isolation).
    """

    __slots__ = (
        # Market
        "market_service",
        "storage_writer",
        "ingestor",
        "indicator_manager",
        # Signal
        "signal_module",
        "signal_runtime",
        "htf_cache",
        "signal_quality_tracker",
        "calibrator",
        "performance_tracker",
        "market_structure_analyzer",
        # Trading
        "trade_registry",
        "trade_module",
        "trade_executor",
        "position_manager",
        "trade_outcome_tracker",
        "pending_entry_manager",
        # Calendar
        "economic_calendar_service",
        "market_impact_analyzer",
        # Monitoring
        "health_monitor",
        "monitoring_manager",
        "pipeline_event_bus",
        "runtime_read_model",
        # Studio
        "studio_service",
        # Runtime cleanup
        "shutdown_callbacks",
    )

    def __init__(self) -> None:
        self.market_service: Optional[MarketDataService] = None
        self.storage_writer: Optional[StorageWriter] = None
        self.ingestor: Optional[BackgroundIngestor] = None
        self.indicator_manager: Optional[UnifiedIndicatorManager] = None

        self.signal_module: Optional[SignalModule] = None
        self.signal_runtime: Optional[SignalRuntime] = None
        self.htf_cache: Optional[HTFStateCache] = None
        self.signal_quality_tracker: Optional[SignalQualityTracker] = None
        self.calibrator: Optional[ConfidenceCalibrator] = None
        self.performance_tracker: Optional[StrategyPerformanceTracker] = None
        self.market_structure_analyzer: Optional[MarketStructureAnalyzer] = None

        self.trade_registry: Optional[TradingAccountRegistry] = None
        self.trade_module: Optional[TradingModule] = None
        self.trade_executor: Optional[TradeExecutor] = None
        self.position_manager: Optional[PositionManager] = None
        self.trade_outcome_tracker: Optional[TradeOutcomeTracker] = None
        self.pending_entry_manager: Optional[PendingEntryManager] = None

        self.economic_calendar_service: Optional[EconomicCalendarService] = None
        self.market_impact_analyzer: Optional[Any] = None

        self.health_monitor: Optional[Any] = None
        self.monitoring_manager: Optional[Any] = None
        self.pipeline_event_bus: Optional[PipelineEventBus] = None
        self.runtime_read_model: Optional[RuntimeReadModel] = None

        self.studio_service: Optional[StudioService] = None
        self.shutdown_callbacks: list[Callable[[], None]] = []
