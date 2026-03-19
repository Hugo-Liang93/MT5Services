from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from .contracts import SESSION_LONDON, SESSION_NEW_YORK


@dataclass
class SignalPolicy:
    min_preview_confidence: float = 0.55
    min_preview_bar_progress: float = 0.2
    min_preview_stable_seconds: float = 15.0
    preview_cooldown_seconds: float = 30.0
    # Minimum wall-clock gap to skip duplicate intrabar snapshots with identical
    # indicator signatures.  Has no effect on confirmed (bar-close) snapshots,
    # which are always deduplicated by (bar_time, signature) regardless of this value.
    snapshot_dedupe_window_seconds: float = 1.0
    max_spread_points: float = 50.0
    allowed_sessions: tuple[str, ...] = (SESSION_LONDON, SESSION_NEW_YORK)
    auto_trade_enabled: bool = False
    auto_trade_min_confidence: float = 0.7
    auto_trade_require_armed: bool = True
    # Pre-flight affinity gate: strategies with regime_affinity < this value
    # for the current Regime are skipped before calling service.evaluate().
    # Avoids wasting CPU on strategies that are clearly unreliable in the
    # current market type.  Set to 0.0 to disable.
    min_affinity_skip: float = 0.15
    # Strategy voting engine settings
    voting_enabled: bool = True
    voting_consensus_threshold: float = 0.40
    voting_min_quorum: int = 2
    voting_disagreement_penalty: float = 0.50
    # 当 confirmed 队列满时，允许短时阻塞等待消费者腾挪队列。
    confirmed_queue_backpressure_timeout_seconds: float = 0.2


@dataclass
class RuntimeSignalState:
    preview_state: str = "idle"
    preview_action: Optional[str] = None
    preview_bar_time: Optional[datetime] = None
    preview_since: Optional[datetime] = None
    confirmed_state: str = "idle"
    confirmed_bar_time: Optional[datetime] = None
    last_emitted_state: Optional[str] = None
    last_emitted_at: Optional[datetime] = None
    last_emitted_bar_time: Optional[datetime] = None
    last_snapshot_scope: Optional[str] = None
    last_snapshot_bar_time: Optional[datetime] = None
    last_snapshot_signature: Optional[Tuple] = None
    last_snapshot_time: Optional[datetime] = None
