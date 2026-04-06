from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..contracts import SESSION_LONDON, SESSION_NEW_YORK


@dataclass(frozen=True)
class VotingGroupConfig:
    """单个 voting group 的配置。

    name:
        该 group 投票结果的信号名称（strategy 字段值），例如 "trend_vote"。
    strategies:
        参与本 group 投票的策略名称集合。只有这些策略的决策参与计票；
        其他策略的决策被忽略（不影响它们独立发出信号）。
    consensus_threshold / min_quorum / disagreement_penalty:
        与全局 voting 参数语义相同，但仅作用于本 group。
    """

    name: str
    strategies: frozenset[str]
    consensus_threshold: float = 0.40
    min_quorum: int = 2
    min_quorum_ratio: float = 0.0  # > 0 时取 max(min_quorum, ceil(total × ratio))
    disagreement_penalty: float = 0.50
    # 策略级投票权重：对高度相关的策略降权（默认 1.0，配置低于 1.0 表示降权）
    strategy_weights: dict[str, float] = field(default_factory=dict)


@dataclass
class SignalPolicy:
    min_preview_confidence: float = 0.55
    min_preview_bar_progress: float = 0.15
    min_preview_stable_seconds: float = 15.0
    preview_cooldown_seconds: float = 30.0
    # Minimum wall-clock gap to skip duplicate intrabar snapshots with identical
    # indicator signatures.  Has no effect on confirmed (bar-close) snapshots,
    # which are always deduplicated by (bar_time, signature) regardless of this value.
    snapshot_dedupe_window_seconds: float = 1.0
    max_spread_points: float = 50.0
    allowed_sessions: tuple[str, ...] = (SESSION_LONDON, SESSION_NEW_YORK)
    # Strategy voting engine settings
    voting_enabled: bool = True
    voting_consensus_threshold: float = 0.40
    voting_min_quorum: int = 2
    voting_disagreement_penalty: float = 0.50
    strategy_sessions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # 每个策略允许运行的时间框架白名单（空 = 允许所有时间框架）。
    # 用于防止为短周期的 M1 分钟级噪声注入周期更长的策略（如 SMA/MACD）。
    strategy_timeframes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # 当 confirmed 队列满时，允许短时阻塞等待消费者腾挪队列。
    confirmed_queue_backpressure_timeout_seconds: float = 0.2
    # ── 多组 Voting 配置 ──────────────────────────────────────────────
    # 每个 VotingGroupConfig 代表一个独立的投票组，产生以 group.name 命名的信号。
    # 非空时，全局单一 consensus 投票自动禁用（被 groups 取代）。
    # 空列表 = 使用全局 consensus 模式。
    voting_groups: list[VotingGroupConfig] = field(default_factory=list)
    # 虽然属于某个 voting group，但仍允许单独触发交易的策略名单（白名单覆盖）。
    standalone_override: frozenset[str] = field(default_factory=frozenset)
    # Indicators that must be present in the snapshot before signal evaluation.
    # Prevents wasting state_changed=true transitions on incomplete data.
    # Empty tuple disables the check (e.g. in tests).
    warmup_required_indicators: tuple[str, ...] = ("atr14",)


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
    last_snapshot_signature: Optional[int] = None
    last_snapshot_time: Optional[datetime] = None
