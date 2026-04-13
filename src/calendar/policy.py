from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EconomicEventWindow:
    lookahead_minutes: int
    lookback_minutes: int
    importance_min: int


@dataclass(frozen=True)
class SignalEconomicPolicy:
    """Signal-domain economic event policy.

    The signal pipeline uses a single policy source for:
    - hard pre-evaluation filtering
    - confidence decay around the same event family

    Account-domain trade guard remains a separate risk policy.
    """

    enabled: bool
    filter_window: EconomicEventWindow
    query_window: EconomicEventWindow
    hard_block_pre_minutes: int
    hard_block_post_minutes: int
    decay_pre_minutes: int
    decay_post_minutes: int

    def decay_for_delta(self, delta_minutes: float) -> float:
        """Map event delta (event_time - now, minutes) to confidence decay."""
        if not self.enabled:
            return 1.0

        if delta_minutes > self.decay_pre_minutes:
            return 1.0

        if delta_minutes > self.hard_block_pre_minutes:
            span = float(self.decay_pre_minutes - self.hard_block_pre_minutes)
            if span <= 0:
                return 0.0
            return max(
                0.0,
                min(1.0, (delta_minutes - self.hard_block_pre_minutes) / span),
            )

        if delta_minutes >= 0:
            return 0.0

        if delta_minutes >= -self.hard_block_post_minutes:
            return 0.0

        if delta_minutes >= -self.decay_post_minutes:
            span = float(self.decay_post_minutes - self.hard_block_post_minutes)
            if span <= 0:
                return 1.0
            distance = abs(delta_minutes) - float(self.hard_block_post_minutes)
            return max(0.0, min(1.0, distance / span))

        return 1.0


def build_signal_economic_policy(settings: Any) -> SignalEconomicPolicy:
    enabled = bool(settings.enabled)
    importance_min = max(1, int(settings.high_importance_threshold))
    hard_block_pre = max(0, int(settings.pre_event_buffer_minutes))
    hard_block_post = max(0, int(settings.post_event_buffer_minutes))
    decay_pre = max(hard_block_pre, int(settings.release_watch_approaching_minutes))
    decay_post = max(hard_block_post, int(settings.release_watch_post_event_minutes))

    return SignalEconomicPolicy(
        enabled=enabled,
        filter_window=EconomicEventWindow(
            lookahead_minutes=hard_block_pre,
            lookback_minutes=hard_block_post,
            importance_min=importance_min,
        ),
        query_window=EconomicEventWindow(
            lookahead_minutes=decay_pre,
            lookback_minutes=decay_post,
            importance_min=importance_min,
        ),
        hard_block_pre_minutes=hard_block_pre,
        hard_block_post_minutes=hard_block_post,
        decay_pre_minutes=decay_pre,
        decay_post_minutes=decay_post,
    )
