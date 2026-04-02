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
    # 时间框架差异化风险乘数：tf → multiplier（覆盖 sizing.py 的默认值）
    timeframe_risk_multipliers: dict[str, float] = Field(default_factory=dict)
    # Per-TF 最低交易置信度：低 TF 噪声多可设更高阈值过滤弱信号
    timeframe_min_confidence: dict[str, float] = Field(default_factory=dict)
    # HTF 方向冲突时强制拒绝交易的 TF 集合
    htf_conflict_block_timeframes: frozenset[str] = Field(default_factory=frozenset)
    # 豁免 HTF 冲突阻止的策略类别（均值回归天然做反向）
    htf_conflict_exempt_categories: frozenset[str] = Field(
        default_factory=lambda: frozenset({"reversion"})
    )
    min_volume: float = 0.01
    max_volume: float = 1.0
    base_spread_points: float = 0.0
    max_spread_multiplier: float = 2.70
    max_spread_points: float = 50.0
    allowed_sessions: str = "london,newyork"
    session_transition_cooldown_minutes: int = 15
    economic_filter_enabled: bool = True
    economic_lookahead_minutes: int = 30
    economic_lookback_minutes: int = 15
    economic_importance_min: int = 3
    min_preview_confidence: float = 0.55
    min_preview_bar_progress: float = 0.15
    preview_stable_seconds: float = 15.0
    preview_cooldown_seconds: float = 30.0
    snapshot_dedupe_window_seconds: float = 0.3
    min_affinity_skip: float = 0.15
    # Delta momentum bonus 参数（均值回归策略共享）
    delta_d3_scale: float = 10.0
    delta_d3_cap: float = 0.05
    delta_d5_threshold: float = 8.0
    delta_d5_bonus: float = 0.03
    soft_regime_enabled: bool = True
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
    # ── Trailing Take Profit ──
    trailing_tp_enabled: bool = False
    trailing_tp_activation_atr: float = 1.5
    trailing_tp_trail_atr: float = 0.8
    # ── 指标驱动出场 ──
    indicator_exit_enabled: bool = False
    indicator_exit_supertrend_enabled: bool = True
    indicator_exit_supertrend_tighten_atr: float = 0.5
    indicator_exit_rsi_enabled: bool = True
    indicator_exit_rsi_overbought: float = 75.0
    indicator_exit_rsi_oversold: float = 25.0
    indicator_exit_rsi_delta_threshold: float = 5.0
    indicator_exit_rsi_tighten_atr: float = 0.5
    indicator_exit_macd_enabled: bool = True
    indicator_exit_macd_tighten_atr: float = 0.5
    indicator_exit_adx_enabled: bool = True
    indicator_exit_adx_entry_min: float = 25.0
    indicator_exit_adx_collapse_threshold: float = 10.0
    indicator_exit_adx_tighten_atr: float = 0.3
    max_consecutive_failures: int = 3
    circuit_auto_reset_minutes: int = 30
    contract_size_map: dict[str, float] = Field(
        default_factory=lambda: {"XAUUSD": 100.0, "default": 100.0}
    )
    session_spread_limits: dict[str, float] = Field(default_factory=dict)
    strategy_sessions: dict[str, list[str]] = Field(default_factory=dict)
    strategy_timeframes: dict[str, list[str]] = Field(default_factory=dict)
    max_spread_to_stop_ratio: float = 0.33
    # 同策略同方向再入场冷却 bar 数（0=不冷却每根 bar 都可以，N=间隔 N 根后允许加仓）
    reentry_cooldown_bars: int = 3
    # 多组 Voting 配置：原始 dict 列表，在工厂层转换为 VotingGroupConfig 对象
    # 每个 dict 包含: name, strategies(list), consensus_threshold, min_quorum, disagreement_penalty
    voting_group_configs: list[dict] = Field(default_factory=list)
    # 即使属于 voting group，仍允许单独触发交易的策略名单（standalone_override 覆盖）
    standalone_override: list[str] = Field(default_factory=list)
    # ── Performance Tracker（日内策略绩效追踪）──
    perf_tracker_enabled: bool = True
    perf_tracker_baseline_win_rate: float = 0.50
    perf_tracker_min_multiplier: float = 0.50
    perf_tracker_max_multiplier: float = 1.20
    perf_tracker_streak_penalty_threshold: int = 3
    perf_tracker_streak_penalty_factor: float = 0.90
    perf_tracker_category_fallback_min_samples: int = 3
    perf_tracker_session_reset_interval_hours: int = 0
    # PnL 熔断器（计实际亏损次数，与 circuit_breaker 的技术故障计数相互独立）
    perf_tracker_pnl_circuit_enabled: bool = True
    perf_tracker_pnl_circuit_max_consecutive_losses: int = 5
    perf_tracker_pnl_circuit_cooldown_minutes: int = 120

    # ── 置信度底线 ──
    confidence_floor: float = 0.10
    # ── HTF Cache ──
    htf_cache_max_age_seconds: int = 14400
    # ── HTF Indicators（跨时间框架指标注入）──
    htf_indicators_enabled: bool = True
    # 策略 HTF 目标 TF 映射：{strategy.running_tf: target_tf}
    # 例：{"supertrend.M5": "H1", "sma_trend.H1": "D1"}
    strategy_htf_targets: dict[str, str] = Field(default_factory=dict)
    # ── Intrabar 置信度缩放因子 ──
    intrabar_confidence_factor: float = 0.85
    # ── HTF 方向对齐修正 ──
    htf_conflict_penalty: float = 0.70
    htf_alignment_boost: float = 1.10
    htf_alignment_strength_coefficient: float = 0.30
    htf_alignment_stability_per_bar: float = 0.03
    htf_alignment_stability_cap: float = 1.15
    htf_alignment_intrabar_strength_ratio: float = 0.50
    # ── 波动率异常过滤 ──
    volatility_atr_spike_multiplier: float = 2.5
    # ── Signal Quality Tracker ──
    signal_quality_bars_to_evaluate: int = 5
    signal_quality_max_pending: int = 500

    # ═══════════════════════════════════════════════════════════════
    # Pending Entry（价格确认入场）
    # ═══════════════════════════════════════════════════════════════
    pending_entry_pullback_atr_factor: float = 0.3
    pending_entry_chase_atr_factor: float = 0.1
    pending_entry_momentum_atr_factor: float = 0.5
    pending_entry_symmetric_atr_factor: float = 0.4
    pending_entry_check_interval: float = 0.5
    pending_entry_max_spread_points: float = 0.0
    pending_entry_default_timeout_bars: float = 2.0
    pending_entry_timeout_bars: dict[str, float] = Field(
        default_factory=lambda: {
            "M1": 3.0, "M5": 2.0, "M15": 1.5,
            "H1": 1.0, "H4": 0.5, "D1": 0.25,
        }
    )
    pending_entry_cancel_on_new_signal: bool = True
    pending_entry_cancel_same_direction: bool = False
    pending_entry_strategy_overrides: dict[str, dict[str, float]] = Field(default_factory=dict)
    pending_entry_tf_overrides: dict[str, dict[str, float]] = Field(default_factory=dict)

    market_structure_enabled: bool = True
    market_structure_lookback_bars: int = 400
    market_structure_m1_lookback_bars: int = 120
    market_structure_open_range_minutes: int = 60
    market_structure_compression_window_bars: int = 6
    market_structure_reference_window_bars: int = 24

    # ═══════════════════════════════════════════════════════════════
    # Regime 检测阈值
    # ═══════════════════════════════════════════════════════════════
    regime_adx_trending_threshold: float = 23.0
    regime_adx_ranging_threshold: float = 18.0
    regime_bb_tight_pct: float = 0.008
    regime_tp_trending: float = 1.20
    regime_tp_ranging: float = 0.80
    regime_tp_breakout: float = 1.10
    regime_tp_uncertain: float = 1.00
    regime_sl_trending: float = 1.00
    regime_sl_ranging: float = 0.90
    regime_sl_breakout: float = 1.10
    regime_sl_uncertain: float = 1.00

    # ═══════════════════════════════════════════════════════════════
    # 策略级可调参数 — [strategy_params] section
    # 键格式: <strategy_name>__<param_name>（双下划线分隔）
    # ═══════════════════════════════════════════════════════════════
    strategy_params: dict[str, float] = Field(default_factory=dict)

    # Per-TF 策略参数覆盖 — [strategy_params.<TF>] section
    # 查找优先级: per-TF 值 → 全局 strategy_params → 策略代码默认值
    strategy_params_per_tf: dict[str, dict[str, float]] = Field(default_factory=dict)

    # ═══════════════════════════════════════════════════════════════
    # Regime 亲和度覆盖 — [regime_affinity.<strategy>] section
    # 键格式: <strategy_name> → {trending, ranging, breakout, uncertain}
    # ═══════════════════════════════════════════════════════════════
    regime_affinity_overrides: dict[str, dict[str, float]] = Field(default_factory=dict)
