from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple


@dataclass
class SignalPolicy:
    min_preview_confidence: float = 0.55
    min_preview_bar_progress: float = 0.2
    min_preview_stable_seconds: float = 15.0
    preview_cooldown_seconds: float = 30.0
    snapshot_dedupe_window_seconds: float = 1.0
    max_spread_points: float = 50.0
    allowed_sessions: tuple[str, ...] = ("london", "newyork")
    auto_trade_enabled: bool = False
    auto_trade_min_confidence: float = 0.7
    auto_trade_require_armed: bool = True


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
