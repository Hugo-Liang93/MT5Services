from __future__ import annotations

from src.app_runtime.factories.signals import build_executor_config
from src.config.models.signal import SignalConfig
from src.signals.contracts import (
    StrategyDeployment,
    StrategyDeploymentStatus,
)
from src.trading.execution import ExecutorConfig


def test_build_executor_config_preserves_htf_conflict_contract() -> None:
    signal_config = SignalConfig(
        auto_trade_enabled=True,
        auto_trade_min_confidence=0.82,
        timeframe_min_confidence={"M5": 0.86},
        htf_conflict_block_timeframes=frozenset({"M5", "M15"}),
        htf_conflict_exempt_categories=frozenset({"reversion", "breakout"}),
    )

    config = build_executor_config(signal_config)

    assert isinstance(config, ExecutorConfig)
    assert config.enabled is True
    assert config.min_confidence == 0.82
    assert config.timeframe_min_confidence == {"M5": 0.86}
    assert config.htf_conflict_block_timeframes == frozenset({"M5", "M15"})
    assert config.htf_conflict_exempt_categories == frozenset(
        {"reversion", "breakout"}
    )


def test_build_executor_config_preserves_strategy_deployments() -> None:
    signal_config = SignalConfig(
        auto_trade_enabled=True,
        strategy_deployments={
            "structured_session_breakout": StrategyDeployment(
                name="structured_session_breakout",
                status=StrategyDeploymentStatus.ACTIVE_GUARDED,
                locked_timeframes=("M30",),
                locked_sessions=("london",),
                min_final_confidence=0.55,
                max_live_positions=1,
                require_pending_entry=True,
                paper_shadow_required=True,
                robustness_tier="tf_specific",
                research_provenance="cand_test",
            )
        },
    )

    config = build_executor_config(signal_config)

    deployment = config.strategy_deployments["structured_session_breakout"]
    assert deployment.status is StrategyDeploymentStatus.ACTIVE_GUARDED
    assert deployment.locked_timeframes == ("M30",)
    assert deployment.locked_sessions == ("london",)
    assert deployment.min_final_confidence == 0.55
    assert deployment.max_live_positions == 1
    assert deployment.require_pending_entry is True
    assert deployment.paper_shadow_required is True
