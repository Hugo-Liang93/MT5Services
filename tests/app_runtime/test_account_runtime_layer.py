from __future__ import annotations

from types import SimpleNamespace

from src.app_runtime.builder_phases.account_runtime import build_account_runtime_layer
from src.app_runtime.container import AppContainer


class _DummyExecutor:
    def __init__(self) -> None:
        self.guard = None

    def set_intrabar_guard(self, guard) -> None:
        self.guard = guard


def test_build_account_runtime_layer_wires_intrabar_guard_for_executor(monkeypatch) -> None:
    container = AppContainer()
    container.market_service = object()
    container.storage_writer = SimpleNamespace()
    container.trade_module = object()
    container.runtime_identity = SimpleNamespace(instance_role="executor")
    container.trading_state_store = None
    container.pipeline_event_bus = None
    container.economic_calendar_service = None
    container.shutdown_callbacks = []

    executor = _DummyExecutor()
    monkeypatch.setattr(
        "src.app_runtime.builder_phases.account_runtime.build_account_runtime_components",
        lambda **kwargs: SimpleNamespace(
            trade_outcome_tracker=object(),
            exposure_closeout_controller=object(),
            position_manager=object(),
            trade_executor=executor,
            pending_entry_manager=object(),
            execution_intent_consumer=object(),
        ),
    )
    monkeypatch.setattr(
        "src.app_runtime.builder_phases.account_runtime.build_performance_tracker_config",
        lambda signal_config: object(),
    )
    monkeypatch.setattr(
        "src.app_runtime.builder_phases.account_runtime.StrategyPerformanceTracker",
        lambda config: object(),
    )
    monkeypatch.setattr(
        "src.app_runtime.builder_phases.account_runtime.MarketRegimeDetector",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        "src.app_runtime.builder_phases.account_runtime.register_signal_hot_reload",
        lambda *args, **kwargs: (lambda: None),
    )
    monkeypatch.setattr(
        "src.risk.runtime.wire_margin_guard",
        lambda **kwargs: None,
    )

    signal_config = SimpleNamespace(
        regime_adx_trending_threshold=25,
        regime_adx_ranging_threshold=20,
        regime_bb_tight_pct=0.1,
        intrabar_trading_enabled=True,
        intrabar_trading_enabled_strategies=("structured_breakout_follow",),
    )

    build_account_runtime_layer(
        container,
        signal_config_loader=lambda: signal_config,
        signal_config=signal_config,
    )

    assert executor.guard is not None
