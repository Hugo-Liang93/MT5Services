"""ExecutionGate: pre-execution admission checks for signal events.

Determines whether a SignalEvent should be forwarded to the trade execution
pipeline.  These are strategy-domain routing decisions — they do NOT overlap
with account-level risk controls (which live in ``src.risk.service``).

Responsibilities (moved here from TradeExecutor to enforce clean boundaries):
  1. Voting-group protection — member strategies cannot trigger trades alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from src.signals.models import SignalEvent

logger = logging.getLogger(__name__)


@dataclass
class ExecutionGateConfig:
    # Voting-group protection: strategies belonging to any voting group.
    # Non-empty ⇒ these strategies are blocked from standalone execution;
    # only the vote-group result signal (strategy = group_name) may trigger.
    voting_group_strategies: frozenset[str] = field(default_factory=frozenset)
    # Override: strategies in this set may still trigger standalone even if
    # they belong to a voting group.
    standalone_override: frozenset[str] = field(default_factory=frozenset)
    # Intrabar 交易门控
    intrabar_trading_enabled: bool = False
    intrabar_enabled_strategies: frozenset[str] = field(default_factory=frozenset)


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

        return True, ""

    def check_intrabar(self, event: SignalEvent) -> tuple[bool, str]:
        """Intrabar 交易专用检查。"""
        if not self.config.intrabar_trading_enabled:
            return False, "intrabar_trading_disabled"
        if event.strategy not in self.config.intrabar_enabled_strategies:
            return False, "strategy_not_intrabar_enabled"
        # 投票组成员不允许 intrabar 独立交易（防护性代码，当前无投票组）
        if (
            self.config.voting_group_strategies
            and event.strategy in self.config.voting_group_strategies
            and event.strategy not in self.config.standalone_override
        ):
            return False, "voting_group_member"
        return True, ""
