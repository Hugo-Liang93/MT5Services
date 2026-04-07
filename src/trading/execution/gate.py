"""ExecutionGate: pre-execution admission checks for signal events.

Determines whether a SignalEvent should be forwarded to the trade execution
pipeline.  These are strategy-domain routing decisions — they do NOT overlap
with account-level risk controls (which live in ``src.risk.service``).

Responsibilities (moved here from TradeExecutor to enforce clean boundaries):
  1. Voting-group protection — member strategies cannot trigger trades alone.
  2. require_armed gate — signal must have been "armed" (stable preview)
     before the confirmed bar to be eligible for execution.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from src.signals.metadata_keys import MetadataKey as MK
from src.signals.models import SignalEvent

logger = logging.getLogger(__name__)


@dataclass
class ExecutionGateConfig:
    require_armed: bool = True
    # Voting-group protection: strategies belonging to any voting group.
    # Non-empty ⇒ these strategies are blocked from standalone execution;
    # only the vote-group result signal (strategy = group_name) may trigger.
    voting_group_strategies: frozenset[str] = field(default_factory=frozenset)
    # Override: strategies in this set may still trigger standalone even if
    # they belong to a voting group.
    standalone_override: frozenset[str] = field(default_factory=frozenset)


class ExecutionGate:
    """Stateless admission filter sitting in front of TradeExecutor."""

    def __init__(self, config: Optional[ExecutionGateConfig] = None) -> None:
        self.config = config or ExecutionGateConfig()

    def check(self, event: SignalEvent) -> tuple[bool, str]:
        """Return (allowed, reason).  *reason* is non-empty when blocked."""

        # ── Voting Group protection ────────────────────────────────────
        if (
            self.config.voting_group_strategies
            and event.strategy in self.config.voting_group_strategies
            and event.strategy not in self.config.standalone_override
        ):
            return False, "voting_group_member"

        # ── require_armed gate ─────────────────────────────────────────
        if self.config.require_armed:
            previous_state = event.metadata.get(MK.PREVIOUS_STATE, "")
            preview_at_close = event.metadata.get(MK.PREVIEW_STATE_AT_CLOSE, "")
            # 精确匹配 armed 状态后缀，避免 "armed_cancel" 等被误判为 armed
            _armed_states = ("armed_buy", "armed_sell")
            prev_is_armed = previous_state in _armed_states
            preview_is_armed = preview_at_close in _armed_states
            if not prev_is_armed and not preview_is_armed:
                return False, "require_armed"

        return True, ""
