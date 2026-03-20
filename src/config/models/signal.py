from __future__ import annotations

from pydantic import BaseModel, Field


class SignalConfig(BaseModel):
    auto_trade_enabled: bool = False
    auto_trade_min_confidence: float = 0.7
    auto_trade_require_armed: bool = True
    max_concurrent_positions_per_symbol: int | None = 3
    sl_atr_multiplier: float = 1.5
    tp_atr_multiplier: float = 3.0
    risk_percent_per_trade: float = 1.0
    min_volume: float = 0.01
    max_volume: float = 1.0
    max_spread_points: float = 50.0
    allowed_sessions: str = "london,newyork"
    session_transition_cooldown_minutes: int = 15
    economic_filter_enabled: bool = True
    economic_lookahead_minutes: int = 30
    economic_lookback_minutes: int = 15
    economic_importance_min: int = 3
    min_preview_confidence: float = 0.55
    min_preview_bar_progress: float = 0.2
    preview_stable_seconds: float = 15.0
    preview_cooldown_seconds: float = 30.0
    snapshot_dedupe_window_seconds: float = 1.0
    min_affinity_skip: float = 0.15
    soft_regime_enabled: bool = False
    voting_enabled: bool = True
    voting_consensus_threshold: float = 0.40
    voting_min_quorum: int = 2
    voting_disagreement_penalty: float = 0.50
    trailing_atr_multiplier: float = 1.0
    breakeven_atr_threshold: float = 1.0
    position_reconcile_interval: float = 10.0
    end_of_day_close_enabled: bool = False
    end_of_day_close_hour_utc: int = 21
    end_of_day_close_minute_utc: int = 0
    max_consecutive_failures: int = 3
    circuit_auto_reset_minutes: int = 30
    contract_size_map: dict[str, float] = Field(
        default_factory=lambda: {"XAUUSD": 100.0, "default": 100.0}
    )
    session_spread_limits: dict[str, float] = Field(default_factory=dict)
    strategy_sessions: dict[str, list[str]] = Field(default_factory=dict)
    strategy_timeframes: dict[str, list[str]] = Field(default_factory=dict)
    max_spread_to_stop_ratio: float = 0.33
    # 交易触发白名单：仅列表内的策略允许触发实际下单（空列表 = 不限制）
    trade_trigger_strategies: list[str] = Field(default_factory=list)
    # 多组 Voting 配置：原始 dict 列表，在工厂层转换为 VotingGroupConfig 对象
    # 每个 dict 包含: name, strategies(list), consensus_threshold, min_quorum, disagreement_penalty
    voting_group_configs: list[dict] = Field(default_factory=list)
    # 即使属于 voting group，仍允许单独触发交易的策略名单（standalone_override 覆盖）
    standalone_override: list[str] = Field(default_factory=list)
    market_structure_enabled: bool = True
    market_structure_lookback_bars: int = 400
    market_structure_m1_lookback_bars: int = 120
    market_structure_open_range_minutes: int = 60
    market_structure_compression_window_bars: int = 6
    market_structure_reference_window_bars: int = 24
