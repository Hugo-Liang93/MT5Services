from __future__ import annotations

from src.app_runtime.factories.signals import build_executor_config
from src.config.models.signal import SignalConfig
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
