from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from src.config.file_manager import get_file_config_manager
from src.market_structure import MarketStructureAnalyzer, MarketStructureConfig
from src.signals.contracts import normalize_session_name
from src.signals.execution.filters import (
    EconomicEventFilter,
    SessionFilter,
    SessionTransitionFilter,
    SignalFilterChain,
    SpreadFilter,
    VolatilitySpikeFilter,
)
from src.signals.evaluation.calibrator import ConfidenceCalibrator
from src.signals.evaluation.performance import (
    PerformanceTrackerConfig,
    StrategyPerformanceTracker,
)

from src.signals.evaluation.regime import MarketRegimeDetector
from src.signals.service import SignalModule
from src.signals.orchestration import SignalPolicy, SignalRuntime, SignalTarget
from src.signals.orchestration.policy import VotingGroupConfig
from src.signals.strategies.adapters import UnifiedIndicatorSourceAdapter
from src.signals.strategies.htf_cache import HTFStateCache
from src.signals.strategies.registry import register_all_strategies
from src.signals.tracking.repository import TimescaleSignalRepository
from src.trading.signal_quality_tracker import SignalQualityTracker
from src.trading.trade_outcome_tracker import TradeOutcomeTracker
from src.trading.position_manager import PositionManager
from src.trading.execution_gate import ExecutionGate, ExecutionGateConfig
from src.trading.pending_entry import PendingEntryConfig, PendingEntryManager
from src.trading.sizing import RegimeSizing
from src.trading.signal_executor import ExecutorConfig, TradeExecutor


import logging as _logging

_factory_logger = _logging.getLogger(__name__)


def _apply_strategy_config_overrides(module: SignalModule, signal_config) -> None:
    """从 signal_config 构建 TFParamResolver 并注入到各策略 + 应用 regime_affinity 覆盖。"""
    from src.signals.evaluation.regime import RegimeType
    from src.signals.strategies.tf_params import build_tf_param_resolver

    strategies = module._strategies

    # ── 构建 per-TF 参数查表器并注入 ─────────────────────────────────
    per_tf = getattr(signal_config, "strategy_params_per_tf", {})
    resolver = build_tf_param_resolver(signal_config.strategy_params, per_tf)
    module._tf_param_resolver = resolver  # type: ignore[attr-defined]
    for strategy in strategies.values():
        strategy._tf_param_resolver = resolver  # type: ignore[attr-defined]
    _factory_logger.info("TFParamResolver built: %s", resolver)

    # ── regime_affinity_overrides 覆盖 ────────────────────────────────
    _regime_map = {
        "trending": RegimeType.TRENDING,
        "ranging": RegimeType.RANGING,
        "breakout": RegimeType.BREAKOUT,
        "uncertain": RegimeType.UNCERTAIN,
    }
    for strategy_name, affinity_dict in signal_config.regime_affinity_overrides.items():
        strategy = strategies.get(strategy_name)
        if strategy is None:
            continue
        for regime_key, weight in affinity_dict.items():
            regime_type = _regime_map.get(regime_key.lower())
            if regime_type is not None and hasattr(strategy, "regime_affinity"):
                strategy.regime_affinity[regime_type] = weight
        _factory_logger.debug(
            "regime_affinity override: %s → %s", strategy_name, strategy.regime_affinity
        )


def build_performance_tracker_config(signal_config) -> PerformanceTrackerConfig:
    return PerformanceTrackerConfig(
        enabled=signal_config.perf_tracker_enabled,
        baseline_win_rate=signal_config.perf_tracker_baseline_win_rate,
        min_multiplier=signal_config.perf_tracker_min_multiplier,
        max_multiplier=signal_config.perf_tracker_max_multiplier,
        streak_penalty_threshold=signal_config.perf_tracker_streak_penalty_threshold,
        streak_penalty_factor=signal_config.perf_tracker_streak_penalty_factor,
        min_samples_for_penalty=signal_config.perf_tracker_min_samples_for_penalty,
        category_fallback_min_samples=signal_config.perf_tracker_category_fallback_min_samples,
        session_reset_interval_hours=signal_config.perf_tracker_session_reset_interval_hours,
        pnl_circuit_enabled=signal_config.perf_tracker_pnl_circuit_enabled,
        pnl_circuit_max_consecutive_losses=signal_config.perf_tracker_pnl_circuit_max_consecutive_losses,
        pnl_circuit_cooldown_minutes=signal_config.perf_tracker_pnl_circuit_cooldown_minutes,
    )


@dataclass(frozen=True)
class SignalComponents:
    calibrator: ConfidenceCalibrator
    market_structure_analyzer: MarketStructureAnalyzer
    signal_module: SignalModule
    signal_runtime: SignalRuntime
    htf_cache: HTFStateCache
    signal_quality_tracker: SignalQualityTracker
    trade_outcome_tracker: TradeOutcomeTracker
    position_manager: PositionManager
    trade_executor: TradeExecutor
    performance_tracker: StrategyPerformanceTracker
    pending_entry_manager: PendingEntryManager


def build_pending_entry_config(signal_config) -> PendingEntryConfig:
    return PendingEntryConfig(
        pullback_atr_factor=signal_config.pending_entry_pullback_atr_factor,
        chase_atr_factor=signal_config.pending_entry_chase_atr_factor,
        momentum_atr_factor=signal_config.pending_entry_momentum_atr_factor,
        symmetric_atr_factor=signal_config.pending_entry_symmetric_atr_factor,
        check_interval=signal_config.pending_entry_check_interval,
        max_spread_points=signal_config.pending_entry_max_spread_points,
        timeout_bars=dict(signal_config.pending_entry_timeout_bars),
        default_timeout_bars=signal_config.pending_entry_default_timeout_bars,
        cancel_on_new_signal=signal_config.pending_entry_cancel_on_new_signal,
        cancel_same_direction=signal_config.pending_entry_cancel_same_direction,
        strategy_overrides=dict(signal_config.pending_entry_strategy_overrides),
        tf_overrides=dict(signal_config.pending_entry_tf_overrides),
    )


def build_signal_filter_chain(signal_config, economic_calendar_service) -> SignalFilterChain:
    return SignalFilterChain(
        session_filter=SessionFilter(
            allowed_sessions=tuple(
                normalize_session_name(session)
                for session in signal_config.allowed_sessions.split(",")
                if session.strip()
            )
        ),
        session_transition_filter=SessionTransitionFilter(
            cooldown_minutes=signal_config.session_transition_cooldown_minutes,
        ),
        spread_filter=SpreadFilter(
            max_spread_points=signal_config.max_spread_points,
            session_max_spread_points=dict(signal_config.session_spread_limits),
        ),
        economic_filter=EconomicEventFilter(
            provider=(
                economic_calendar_service if signal_config.economic_filter_enabled else None
            ),
            lookahead_minutes=signal_config.economic_lookahead_minutes,
            lookback_minutes=signal_config.economic_lookback_minutes,
            importance_min=signal_config.economic_importance_min,
        ),
        volatility_filter=VolatilitySpikeFilter(
            spike_multiplier=signal_config.volatility_atr_spike_multiplier,
        ),
    )


def build_executor_config(signal_config) -> ExecutorConfig:
    return ExecutorConfig(
        enabled=signal_config.auto_trade_enabled,
        min_confidence=signal_config.auto_trade_min_confidence,
        max_concurrent_positions_per_symbol=signal_config.max_concurrent_positions_per_symbol,
        risk_percent=signal_config.risk_percent_per_trade,
        sl_atr_multiplier=signal_config.sl_atr_multiplier,
        tp_atr_multiplier=signal_config.tp_atr_multiplier,
        min_volume=signal_config.min_volume,
        max_volume=signal_config.max_volume,
        contract_size_map=dict(signal_config.contract_size_map),
        timeframe_risk_multipliers=dict(signal_config.timeframe_risk_multipliers),
        timeframe_min_confidence=dict(
            getattr(signal_config, "timeframe_min_confidence", {}) or {}
        ),
        htf_conflict_block_timeframes=frozenset(
            getattr(signal_config, "htf_conflict_block_timeframes", frozenset()) or frozenset()
        ),
        htf_conflict_exempt_categories=frozenset(
            getattr(signal_config, "htf_conflict_exempt_categories", frozenset())
            or frozenset({"reversion"})
        ),
        max_consecutive_failures=signal_config.max_consecutive_failures,
        circuit_auto_reset_minutes=signal_config.circuit_auto_reset_minutes,
        max_spread_to_stop_ratio=signal_config.max_spread_to_stop_ratio,
        reentry_cooldown_bars=int(
            getattr(signal_config, "reentry_cooldown_bars", 3) or 3
        ),
        regime_sizing=RegimeSizing(
            tp_trending=signal_config.regime_tp_trending,
            tp_ranging=signal_config.regime_tp_ranging,
            tp_breakout=signal_config.regime_tp_breakout,
            tp_uncertain=signal_config.regime_tp_uncertain,
            sl_trending=signal_config.regime_sl_trending,
            sl_ranging=signal_config.regime_sl_ranging,
            sl_breakout=signal_config.regime_sl_breakout,
            sl_uncertain=signal_config.regime_sl_uncertain,
        ),
    )


def build_execution_gate_config(signal_config) -> ExecutionGateConfig:
    voting_group_strategies: frozenset[str] = frozenset(
        strategy
        for cfg in signal_config.voting_group_configs
        for strategy in cfg.get("strategies", [])
    )
    return ExecutionGateConfig(
        require_armed=signal_config.auto_trade_require_armed,
        voting_group_strategies=voting_group_strategies,
        standalone_override=frozenset(signal_config.standalone_override),
    )


def build_signal_policy(signal_config) -> SignalPolicy:
    allowed_sessions = tuple(
        normalize_session_name(session)
        for session in signal_config.allowed_sessions.split(",")
        if session.strip()
    )
    strategy_sessions = {
        strategy_name: tuple(
            normalize_session_name(session)
            for session in sessions
            if str(session).strip()
        )
        for strategy_name, sessions in signal_config.strategy_sessions.items()
    }
    strategy_timeframes = {
        strategy_name: tuple(str(tf).strip().upper() for tf in tfs if str(tf).strip())
        for strategy_name, tfs in signal_config.strategy_timeframes.items()
    }
    # 将 voting_group_configs（raw dicts）转换为 VotingGroupConfig 对象
    voting_groups = [
        VotingGroupConfig(
            name=cfg["name"],
            strategies=frozenset(cfg["strategies"]),
            consensus_threshold=cfg.get("consensus_threshold", 0.40),
            min_quorum=cfg.get("min_quorum", 2),
            min_quorum_ratio=cfg.get("min_quorum_ratio", 0.0),
            disagreement_penalty=cfg.get("disagreement_penalty", 0.50),
        )
        for cfg in signal_config.voting_group_configs
        if cfg.get("name") and cfg.get("strategies")
    ]
    return SignalPolicy(
        min_preview_confidence=signal_config.min_preview_confidence,
        min_preview_bar_progress=signal_config.min_preview_bar_progress,
        min_preview_stable_seconds=signal_config.preview_stable_seconds,
        preview_cooldown_seconds=signal_config.preview_cooldown_seconds,
        snapshot_dedupe_window_seconds=signal_config.snapshot_dedupe_window_seconds,
        max_spread_points=signal_config.max_spread_points,
        allowed_sessions=allowed_sessions,
        min_affinity_skip=signal_config.min_affinity_skip,
        voting_enabled=signal_config.voting_enabled,
        voting_consensus_threshold=signal_config.voting_consensus_threshold,
        voting_min_quorum=signal_config.voting_min_quorum,
        voting_disagreement_penalty=signal_config.voting_disagreement_penalty,
        strategy_sessions=strategy_sessions,
        strategy_timeframes=strategy_timeframes,
        voting_groups=voting_groups,
        standalone_override=frozenset(signal_config.standalone_override),
    )


def build_signal_components(
    *,
    indicator_manager,
    storage_writer,
    trade_module,
    economic_calendar_service,
    signal_config,
    trading_state_store=None,
) -> SignalComponents:
    market_structure_analyzer = MarketStructureAnalyzer(
        indicator_manager.market_service,
        config=MarketStructureConfig(
            enabled=signal_config.market_structure_enabled,
            lookback_bars=signal_config.market_structure_lookback_bars,
            m1_lookback_bars=signal_config.market_structure_m1_lookback_bars,
            open_range_minutes=signal_config.market_structure_open_range_minutes,
            compression_window_bars=signal_config.market_structure_compression_window_bars,
            compression_reference_bars=signal_config.market_structure_reference_window_bars,
        ),
    )
    # ── Regime 检测器（使用配置化阈值）─────────────────────────────────
    regime_detector = MarketRegimeDetector(
        adx_trending_threshold=signal_config.regime_adx_trending_threshold,
        adx_ranging_threshold=signal_config.regime_adx_ranging_threshold,
        bb_tight_pct=signal_config.regime_bb_tight_pct,
    )
    calibrator = ConfidenceCalibrator(
        fetch_winrates_fn=storage_writer.db.fetch_winrates,
        alpha=0.15,
        baseline_win_rate=0.50,
        max_boost=1.30,
        min_samples=100,
        full_alpha_min_samples=200,
        refresh_interval_seconds=900,
        recency_hours=8,
    )
    performance_tracker = StrategyPerformanceTracker(
        config=build_performance_tracker_config(signal_config),
    )
    signal_module = SignalModule(
        indicator_source=UnifiedIndicatorSourceAdapter(indicator_manager),
        repository=TimescaleSignalRepository(storage_writer.db),
        calibrator=calibrator,
        performance_tracker=performance_tracker,
        soft_regime_enabled=signal_config.soft_regime_enabled,
        confidence_floor=signal_config.confidence_floor,
        regime_detector=regime_detector,
    )

    # 根据当前实际配置的时间框架，自动构建完整的 LTF→HTF 映射。
    # 每个已配置的 TF 映射到链条中下一个已配置的更高 TF。
    # 例如 configured = {M5,M15,M30,H1,H4,D1} → M5→M15, M15→M30, M30→H1, H1→H4, H4→D1
    configured_tfs = set(indicator_manager.config.timeframes)
    _tf_chain = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
    htf_map: dict[str, str] = {}
    for i, tf in enumerate(_tf_chain):
        if tf not in configured_tfs:
            continue
        # 找到链条中下一个已配置的更高 TF
        for j in range(i + 1, len(_tf_chain)):
            if _tf_chain[j] in configured_tfs:
                htf_map[tf] = _tf_chain[j]
                break
    # 多组投票模式下，HTFStateCache 需监听 voting group 信号而非 consensus。
    # 自动推导 source_strategies：有 voting_groups 时用 group name，否则用 "consensus"。
    htf_source_strategies: frozenset[str] | None = None
    if signal_config.voting_group_configs:
        group_names = frozenset(
            cfg["name"]
            for cfg in signal_config.voting_group_configs
            if cfg.get("name")
        )
        if group_names:
            htf_source_strategies = group_names
    htf_cache = HTFStateCache(
        htf_map=htf_map if htf_map else None,
        max_age_seconds=signal_config.htf_cache_max_age_seconds,
        source_strategies=htf_source_strategies,
    )
    register_all_strategies(signal_module, htf_cache)

    # ── Delta momentum 全局参数配置 ─────────────────────────────────────
    from src.signals.strategies.mean_reversion import configure_delta_params
    configure_delta_params(
        d3_scale=signal_config.delta_d3_scale,
        d3_cap=signal_config.delta_d3_cap,
        d5_threshold=signal_config.delta_d5_threshold,
        d5_bonus=signal_config.delta_d5_bonus,
    )

    # ── 应用配置化参数覆盖 ────────────────────────────────────────────
    _apply_strategy_config_overrides(signal_module, signal_config)

    # ── HTF INI 配置校验（硬错误，阻止启动）─────────────────────────
    _enabled_indicators = {
        cfg.name for cfg in indicator_manager.config.indicators if cfg.enabled
    }
    _registered_strategies = set(signal_module.list_strategies())
    _htf_errors: list[str] = []
    for compound_key, tf_value in signal_config.strategy_htf_targets.items():
        parts = compound_key.split(".", 1)
        if len(parts) != 2:
            continue
        strategy_name, ind_name = parts[0].strip(), parts[1].strip()
        tf = tf_value.strip().upper()
        if strategy_name not in _registered_strategies:
            _htf_errors.append(
                f"[strategy_htf] {compound_key}: strategy '{strategy_name}' not registered"
            )
        if tf not in configured_tfs:
            _htf_errors.append(
                f"[strategy_htf] {compound_key} = {tf}: timeframe '{tf}' not in app.ini"
            )
        if ind_name not in _enabled_indicators:
            _htf_errors.append(
                f"[strategy_htf] {compound_key} = {tf}: indicator '{ind_name}' not enabled in indicators.json"
            )
    if _htf_errors:
        for err in _htf_errors:
            _factory_logger.error(err)
        raise ValueError(
            f"signal.ini [strategy_htf] has {len(_htf_errors)} invalid entries: "
            + "; ".join(_htf_errors[:3])
            + ("..." if len(_htf_errors) > 3 else "")
        )

    # 从策略的 preferred_scopes + required_indicators 自动推导 intrabar 指标集合，
    # 注入到 indicator_manager。
    indicator_manager.set_intrabar_eligible_override(
        signal_module.intrabar_required_indicators()
    )
    indicator_manager.set_priority_indicator_groups(
        signal_module.required_indicator_groups()
    )
    runtime_targets = [
        SignalTarget(symbol=symbol, timeframe=timeframe, strategy=strategy)
        for symbol in indicator_manager.config.symbols
        for timeframe in indicator_manager.config.timeframes
        for strategy in signal_module.list_strategies()
    ]

    signal_policy = build_signal_policy(signal_config)
    filter_chain = build_signal_filter_chain(signal_config, economic_calendar_service)
    signal_runtime = SignalRuntime(
        service=signal_module,
        snapshot_source=indicator_manager,
        targets=runtime_targets,
        enable_confirmed_snapshot=True,
        enable_intrabar=True,
        policy=signal_policy,
        filter_chain=filter_chain,
        market_structure_analyzer=market_structure_analyzer,
        regime_detector=regime_detector,
        htf_indicators_enabled=signal_config.htf_indicators_enabled,
        intrabar_confidence_factor=signal_config.intrabar_confidence_factor,
        htf_direction_fn=htf_cache.get_htf_direction,
        htf_context_fn=htf_cache.get_htf_context,
        htf_conflict_penalty=signal_config.htf_conflict_penalty,
        htf_alignment_boost=signal_config.htf_alignment_boost,
        htf_alignment_strength_coefficient=signal_config.htf_alignment_strength_coefficient,
        htf_alignment_stability_per_bar=signal_config.htf_alignment_stability_per_bar,
        htf_alignment_stability_cap=signal_config.htf_alignment_stability_cap,
        htf_alignment_intrabar_strength_ratio=signal_config.htf_alignment_intrabar_strength_ratio,
        htf_target_config=dict(signal_config.strategy_htf_targets),
    )

    from src.trading.position_rules import IndicatorExitConfig
    indicator_exit_cfg = IndicatorExitConfig(
        enabled=signal_config.indicator_exit_enabled,
        supertrend_enabled=signal_config.indicator_exit_supertrend_enabled,
        supertrend_tighten_atr=signal_config.indicator_exit_supertrend_tighten_atr,
        rsi_enabled=signal_config.indicator_exit_rsi_enabled,
        rsi_overbought=signal_config.indicator_exit_rsi_overbought,
        rsi_oversold=signal_config.indicator_exit_rsi_oversold,
        rsi_delta_threshold=signal_config.indicator_exit_rsi_delta_threshold,
        rsi_tighten_atr=signal_config.indicator_exit_rsi_tighten_atr,
        macd_enabled=signal_config.indicator_exit_macd_enabled,
        macd_tighten_atr=signal_config.indicator_exit_macd_tighten_atr,
        adx_enabled=signal_config.indicator_exit_adx_enabled,
        adx_entry_min=signal_config.indicator_exit_adx_entry_min,
        adx_collapse_threshold=signal_config.indicator_exit_adx_collapse_threshold,
        adx_tighten_atr=signal_config.indicator_exit_adx_tighten_atr,
    )

    position_manager = PositionManager(
        trading_module=trade_module,
        trailing_atr_multiplier=signal_config.trailing_atr_multiplier,
        breakeven_atr_threshold=signal_config.breakeven_atr_threshold,
        end_of_day_close_enabled=signal_config.end_of_day_close_enabled,
        end_of_day_close_hour_utc=signal_config.end_of_day_close_hour_utc,
        end_of_day_close_minute_utc=signal_config.end_of_day_close_minute_utc,
        trailing_tp_enabled=signal_config.trailing_tp_enabled,
        trailing_tp_activation_atr=signal_config.trailing_tp_activation_atr,
        trailing_tp_trail_atr=signal_config.trailing_tp_trail_atr,
        indicator_exit_config=indicator_exit_cfg,
    )
    # 交易结果追踪器：追踪实际执行的交易盈亏（由 TradeExecutor 登记，PositionManager 关仓评估）
    trade_outcome_tracker = TradeOutcomeTracker(
        write_fn=storage_writer.db.write_trade_outcomes,
        on_outcome_fn=performance_tracker.record_outcome,
    )

    persist_execution_fn = getattr(storage_writer.db, "write_auto_executions", None)
    # signal_quality_tracker 在 trade_executor 之后创建，
    # 使用延迟绑定 lambda 确保引用正确。
    _skip_callback_holder: list = []

    execution_gate = ExecutionGate(config=build_execution_gate_config(signal_config))
    # PendingEntryManager: 价格确认入场
    pending_entry_config = build_pending_entry_config(signal_config)
    # execute_fn 延迟绑定：TradeExecutor._execute 在创建后才可引用
    _executor_holder: list = []
    pending_entry_manager = PendingEntryManager(
        config=pending_entry_config,
        market_service=indicator_manager.market_service,
        execute_fn=lambda event, params, cost: (
            _executor_holder[0]._execute(event, params, cost_metrics=cost)
            if _executor_holder
            else None
        ),
        on_expired_fn=lambda sid, reason: (
            _skip_callback_holder[0](sid, reason) if _skip_callback_holder else None
        ),
        inspect_mt5_order_fn=lambda info: (
            _executor_holder[0]._inspect_pending_mt5_order(info)
            if _executor_holder
            else {"status": "pending"}
        ),
    )
    trade_executor = TradeExecutor(
        trading_module=trade_module,
        config=build_executor_config(signal_config),
        position_manager=position_manager,
        persist_execution_fn=persist_execution_fn,
        trade_outcome_tracker=trade_outcome_tracker,
        on_execution_skip=lambda sid, reason: (
            _skip_callback_holder[0](sid, reason) if _skip_callback_holder else None
        ),
        execution_gate=execution_gate,
        pending_entry_manager=pending_entry_manager,
        performance_tracker=performance_tracker,
    )
    _executor_holder.append(trade_executor)
    signal_runtime.add_signal_listener(trade_executor.on_signal_event)
    htf_cache.attach(signal_runtime)

    # 策略级 intrabar 置信度衰减覆盖（从 strategy_params *__intrabar_decay 提取）
    _decay_overrides: dict[str, float] = {}
    for key, value in signal_config.strategy_params.items():
        if key.endswith("__intrabar_decay"):
            strategy_name = key[: -len("__intrabar_decay")]
            try:
                _decay_overrides[strategy_name] = float(value)
            except (TypeError, ValueError):
                pass
    if _decay_overrides:
        signal_runtime.set_strategy_intrabar_decay(_decay_overrides)

    # 接线：PositionManager 关仓时通知 TradeOutcomeTracker
    position_manager.add_close_callback(trade_outcome_tracker.on_position_closed)
    position_manager.set_recovery_hooks(
        position_context_resolver=lambda ticket, comment: trade_module.resolve_position_context(
            ticket=ticket,
            comment=comment,
        ),
        position_state_resolver=(
            trading_state_store.resolve_position_state
            if trading_state_store is not None
            else None
        ),
        recovered_position_callback=trade_outcome_tracker.restore_tracked_position,
    )
    if trading_state_store is not None:
        pending_entry_manager.set_mt5_order_lifecycle_hooks(
            on_tracked=trading_state_store.record_pending_order_placed,
            on_filled=lambda info, state: trading_state_store.mark_pending_order_filled(
                info,
                state=state,
            ),
            on_expired=lambda info, reason: trading_state_store.mark_pending_order_expired(
                info,
                reason=reason,
            ),
            on_cancelled=lambda info, reason: trading_state_store.mark_pending_order_cancelled(
                info,
                reason=reason,
            ),
            on_missing=lambda info, reason: trading_state_store.mark_pending_order_missing(
                info,
                reason=reason,
            ),
        )
        position_manager.set_state_hooks(
            on_position_tracked=trading_state_store.record_position_tracked,
            on_position_updated=trading_state_store.record_position_update,
            on_position_closed=trading_state_store.mark_position_closed,
        )

    # 信号质量追踪器：N bars 后评估信号预测质量（供 Calibrator 长期统计校准）
    signal_quality_tracker = SignalQualityTracker(
        write_fn=storage_writer.db.write_outcome_events,
        bars_to_evaluate=signal_config.signal_quality_bars_to_evaluate,
        max_pending=signal_config.signal_quality_max_pending,
        on_quality_fn=performance_tracker.record_outcome,
    )
    signal_quality_tracker.attach(signal_runtime)
    # 绑定延迟引用：TradeExecutor skip → SignalQualityTracker.on_execution_skip
    _skip_callback_holder.append(signal_quality_tracker.on_execution_skip)

    return SignalComponents(
        calibrator=calibrator,
        market_structure_analyzer=market_structure_analyzer,
        signal_module=signal_module,
        signal_runtime=signal_runtime,
        htf_cache=htf_cache,
        signal_quality_tracker=signal_quality_tracker,
        trade_outcome_tracker=trade_outcome_tracker,
        position_manager=position_manager,
        trade_executor=trade_executor,
        performance_tracker=performance_tracker,
        pending_entry_manager=pending_entry_manager,
    )


def register_signal_hot_reload(
    signal_runtime,
    signal_config_loader,
    *,
    trade_executor=None,
    economic_calendar_service=None,
    market_structure_analyzer=None,
    performance_tracker=None,
    pending_entry_manager=None,
) -> Callable[[], None]:
    def _on_signal_config_change(filename: str) -> None:
        if filename != "signal.ini":
            return
        signal_config = signal_config_loader()
        signal_runtime.update_policy(build_signal_policy(signal_config))
        signal_runtime.filter_chain = build_signal_filter_chain(
            signal_config,
            economic_calendar_service,
        )
        if trade_executor is not None:
            trade_executor.config = build_executor_config(signal_config)
            trade_executor._execution_gate.config = build_execution_gate_config(signal_config)
        if performance_tracker is not None:
            performance_tracker._config = build_performance_tracker_config(signal_config)
        if pending_entry_manager is not None:
            pending_entry_manager.config = build_pending_entry_config(signal_config)
        if market_structure_analyzer is not None:
            market_structure_analyzer.config = MarketStructureConfig(
                enabled=signal_config.market_structure_enabled,
                lookback_bars=signal_config.market_structure_lookback_bars,
                m1_lookback_bars=signal_config.market_structure_m1_lookback_bars,
                open_range_minutes=signal_config.market_structure_open_range_minutes,
                compression_window_bars=signal_config.market_structure_compression_window_bars,
                compression_reference_bars=signal_config.market_structure_reference_window_bars,
            )

    manager = get_file_config_manager()
    manager.register_change_callback(_on_signal_config_change)

    def _cleanup() -> None:
        manager.unregister_change_callback(_on_signal_config_change)

    return _cleanup
