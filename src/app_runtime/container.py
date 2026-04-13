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
from src.signals.orchestration.runtime import SignalRuntime
from src.signals.service import SignalModule
from src.signals.strategies.htf_cache import HTFStateCache
from src.trading.application.module import TradingModule
from src.trading.commands.consumer import OperatorCommandConsumer
from src.trading.commands.service import OperatorCommandService
from src.trading.pending import PendingEntryManager
from src.trading.positions import PositionManager
from src.trading.closeout import ExposureCloseoutController
from src.trading.execution.executor import TradeExecutor
from src.trading.runtime.registry import TradingAccountRegistry
from src.trading.tracking import SignalQualityTracker
from src.trading.state import TradingStateAlerts
from src.trading.state import TradingStateRecovery
from src.trading.state import TradingStateRecoveryPolicy
from src.trading.state import TradingStateStore
from src.monitoring.pipeline import PipelineEventBus
from src.monitoring.pipeline import PipelineTraceRecorder
from src.readmodels.runtime import RuntimeReadModel
from src.readmodels.trade_trace import TradingFlowTraceReadModel
from src.trading.tracking import TradeOutcomeTracker
from src.backtesting.paper_trading.bridge import PaperTradingBridge
from src.backtesting.paper_trading.tracker import PaperTradeTracker
from src.studio.service import StudioService
from src.app_runtime.lifecycle import RuntimeComponentRegistry
from src.app_runtime.mode_controller import RuntimeModeController
from src.app_runtime.mode_policy import (
    RuntimeModeAutoTransitionPolicy,
    RuntimeModeTransitionGuard,
)
from src.config.runtime_identity import RuntimeIdentity


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
        "regime_detector",
        "performance_tracker",
        "signal_performance_tracker",
        "execution_performance_tracker",
        "market_structure_analyzer",
        # Trading
        "trade_registry",
        "trade_module",
        "trade_executor",
        "exposure_closeout_controller",
        "position_manager",
        "trade_outcome_tracker",
        "pending_entry_manager",
        "execution_intent_publisher",
        "execution_intent_consumer",
        "operator_command_service",
        "operator_command_consumer",
        "trading_state_store",
        "trading_state_alerts",
        "trading_state_recovery",
        "trading_state_recovery_policy",
        # Calendar
        "economic_calendar_service",
        "market_impact_analyzer",
        # Monitoring
        "health_monitor",
        "monitoring_manager",
        "pipeline_event_bus",
        "pipeline_trace_recorder",
        "runtime_read_model",
        "trade_trace_read_model",
        "runtime_component_registry",
        "runtime_mode_guard",
        "runtime_mode_auto_policy",
        "runtime_mode_controller",
        "account_risk_state_projector",
        # Paper Trading
        "paper_trading_bridge",
        "paper_trade_tracker",
        # Studio
        "studio_service",
        # Runtime identity
        "runtime_identity",
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
        self.regime_detector: Optional[Any] = None
        self.performance_tracker: Optional[StrategyPerformanceTracker] = None
        self.signal_performance_tracker: Optional[StrategyPerformanceTracker] = None
        self.execution_performance_tracker: Optional[StrategyPerformanceTracker] = None
        self.market_structure_analyzer: Optional[MarketStructureAnalyzer] = None

        self.trade_registry: Optional[TradingAccountRegistry] = None
        self.trade_module: Optional[TradingModule] = None
        self.trade_executor: Optional[TradeExecutor] = None
        self.exposure_closeout_controller: Optional[ExposureCloseoutController] = None
        self.position_manager: Optional[PositionManager] = None
        self.trade_outcome_tracker: Optional[TradeOutcomeTracker] = None
        self.pending_entry_manager: Optional[PendingEntryManager] = None
        self.execution_intent_publisher: Optional[Any] = None
        self.execution_intent_consumer: Optional[Any] = None
        self.operator_command_service: Optional[OperatorCommandService] = None
        self.operator_command_consumer: Optional[OperatorCommandConsumer] = None
        self.trading_state_store: Optional[TradingStateStore] = None
        self.trading_state_alerts: Optional[TradingStateAlerts] = None
        self.trading_state_recovery: Optional[TradingStateRecovery] = None
        self.trading_state_recovery_policy: Optional[TradingStateRecoveryPolicy] = None

        self.economic_calendar_service: Optional[EconomicCalendarService] = None
        self.market_impact_analyzer: Optional[Any] = None

        self.health_monitor: Optional[Any] = None
        self.monitoring_manager: Optional[Any] = None
        self.pipeline_event_bus: Optional[PipelineEventBus] = None
        self.pipeline_trace_recorder: Optional[PipelineTraceRecorder] = None
        self.runtime_read_model: Optional[RuntimeReadModel] = None
        self.trade_trace_read_model: Optional[TradingFlowTraceReadModel] = None
        self.runtime_component_registry: Optional[RuntimeComponentRegistry] = None
        self.runtime_mode_guard: Optional[RuntimeModeTransitionGuard] = None
        self.runtime_mode_auto_policy: Optional[RuntimeModeAutoTransitionPolicy] = None
        self.runtime_mode_controller: Optional[RuntimeModeController] = None
        self.account_risk_state_projector: Optional[Any] = None

        self.paper_trading_bridge: Optional[PaperTradingBridge] = None
        self.paper_trade_tracker: Optional[PaperTradeTracker] = None

        self.studio_service: Optional[StudioService] = None
        self.runtime_identity: Optional[RuntimeIdentity] = None
        self.shutdown_callbacks: list[Callable[[], None]] = []
