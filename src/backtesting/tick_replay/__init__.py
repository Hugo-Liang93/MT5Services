"""Tick-derived replay infrastructure.

This package is intentionally separate from the OHLC backtest engine. It replays
persisted tick facts, derives the same feature snapshots as live runtime, and
uses bid/ask side prices for execution-cost checks.
"""

from .data_loader import TickReplayDataLoader
from .execution import BidAskTickExecutionModel, TickReplayFill, TickReplayOrder
from .recovery_canary import (
    RecoveryCanaryGate,
    RecoveryCanaryGateDecision,
    RecoveryCanaryGatePolicy,
)
from .recovery import RecoveryReplayFill, RecoveryReplayReport, TickRecoveryReplayRunner
from .runner import TickReplayReport, TickReplayRunner

__all__ = [
    "BidAskTickExecutionModel",
    "RecoveryCanaryGate",
    "RecoveryCanaryGateDecision",
    "RecoveryCanaryGatePolicy",
    "RecoveryReplayFill",
    "RecoveryReplayReport",
    "TickReplayDataLoader",
    "TickReplayFill",
    "TickReplayOrder",
    "TickRecoveryReplayRunner",
    "TickReplayReport",
    "TickReplayRunner",
]
