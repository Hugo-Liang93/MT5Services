from __future__ import annotations

import logging as _logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from src.config.file_manager import get_file_config_manager
from src.market_structure import MarketStructureAnalyzer, MarketStructureConfig
from src.signals.contracts import normalize_session_name
from src.signals.evaluation.calibrator import ConfidenceCalibrator
from src.signals.evaluation.performance import (
    PerformanceTrackerConfig,
    StrategyPerformanceTracker,
)
from src.signals.evaluation.regime import MarketRegimeDetector
from src.signals.execution.filters import (
    EconomicEventFilter,
    SessionFilter,
    SessionTransitionFilter,
    SignalFilterChain,
    SpreadFilter,
    TrendExhaustionFilter,
    VolatilitySpikeFilter,
)
from src.signals.orchestration import SignalPolicy, SignalRuntime, SignalTarget
from src.signals.orchestration.policy import VotingGroupConfig
from src.signals.service import SignalModule
from src.signals.strategies.adapters import UnifiedIndicatorSourceAdapter
from src.signals.strategies.catalog import build_default_strategy_set
from src.signals.strategies.htf_cache import HTFStateCache
from src.signals.tracking.repository import TimescaleSignalRepository
from src.trading.closeout import ExposureCloseoutController, ExposureCloseoutService
from src.trading.execution import (
    ExecutionGate,
    ExecutionGateConfig,
    ExecutorConfig,
    RegimeSizing,
    TradeExecutor,
)
from src.trading.execution.eventing import execute_market_order
from src.trading.execution.pending_orders import inspect_pending_mt5_order
from src.trading.pending import PendingEntryConfig, PendingEntryManager
from src.trading.positions import PositionManager
from src.trading.tracking import SignalQualityTracker, TradeOutcomeTracker

_factory_logger = _logging.getLogger(__name__)


def _apply_strategy_config_overrides(module: SignalModule, signal_config) -> None:
    """从 signal_config 构建 TFParamResolver 并注入到各策略 + 应用 regime_affinity 覆盖。"""
    from src.signals.evaluation.regime import RegimeType
    from src.signals.strategies.tf_params import build_tf_param_resolver

    strategies = module.strategies

    # ── 构建 per-TF 参数查表器并注入 ─────────────────────────────────
    per_tf = getattr(signal_config, "strategy_params_per_tf", {})
    resolver = build_tf_param_resolver(signal_config.strategy_params, per_tf)
    module.set_tf_param_resolver(resolver)
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
        regime_affinity = getattr(strategy, "regime_affinity", None)
        if not isinstance(regime_affinity, Mapping):
            _factory_logger.debug(
                "strategy %s does not expose mutable regime_affinity; skip overrides",
                strategy_name,
            )
            continue
        for regime_key, weight in affinity_dict.items():
            regime_type = _regime_map.get(regime_key.lower())
            if regime_type is not None:
                regime_affinity[regime_type] = weight
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
    regime_detector: MarketRegimeDetector
    market_structure_analyzer: MarketStructureAnalyzer
    signal_module: SignalModule
    signal_runtime: SignalRuntime
    htf_cache: HTFStateCache
    signal_quality_tracker: SignalQualityTracker
    trade_outcome_tracker: TradeOutcomeTracker
    exposure_closeout_controller: ExposureCloseoutController
    position_manager: PositionManager
    trade_executor: TradeExecutor
    performance_tracker: StrategyPerformanceTracker
    pending_entry_manager: PendingEntryManager


def build_pending_entry_config(signal_config) -> PendingEntryConfig:
    return PendingEntryConfig(
        check_interval=signal_config.pending_entry_check_interval,
        max_spread_points=signal_config.pending_entry_max_spread_points,
        timeout_bars=dict(signal_config.pending_entry_timeout_bars),
        default_timeout_bars=signal_config.pending_entry_default_timeout_bars,
        cancel_on_new_signal=signal_config.pending_entry_cancel_on_new_signal,
        cancel_same_direction=signal_config.pending_entry_cancel_same_direction,
    )


def build_signal_filter_chain(
    signal_config, economic_calendar_service
) -> SignalFilterChain:
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
                economic_calendar_service
                if signal_config.economic_filter_enabled
                else None
            ),
            lookahead_minutes=signal_config.economic_lookahead_minutes,
            lookback_minutes=signal_config.economic_lookback_minutes,
            importance_min=signal_config.economic_importance_min,
        ),
        volatility_filter=VolatilitySpikeFilter(
            spike_multiplier=signal_config.volatility_atr_spike_multiplier,
        ),
        trend_exhaustion_filter=TrendExhaustionFilter(),
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
            getattr(signal_config, "htf_conflict_block_timeframes", frozenset())
            or frozenset()
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
    intrabar_enabled_strategies = frozenset(
        s
        for s in getattr(signal_config, "intrabar_trading_enabled_strategies", [])
        if s
    )
    return ExecutionGateConfig(
        voting_group_strategies=voting_group_strategies,
        standalone_override=frozenset(signal_config.standalone_override),
        intrabar_trading_enabled=getattr(
            signal_config, "intrabar_trading_enabled", False
        ),
        intrabar_enabled_strategies=intrabar_enabled_strategies,
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
            strategy_weights=cfg.get("strategy_weights", {}),
        )
        for cfg in signal_config.voting_group_configs
        if cfg.get("name") and cfg.get("strategies")
    ]
    return SignalPolicy(
        snapshot_dedupe_window_seconds=signal_config.snapshot_dedupe_window_seconds,
        max_spread_points=signal_config.max_spread_points,
        allowed_sessions=allowed_sessions,
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
    pipeline_event_bus=None,
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
        alpha=float(signal_config.calibrator_alpha),
        baseline_win_rate=float(signal_config.calibrator_baseline_win_rate),
        max_boost=float(signal_config.calibrator_max_boost),
        min_samples=int(signal_config.calibrator_min_samples),
        full_alpha_min_samples=int(signal_config.calibrator_full_alpha_min_samples),
        refresh_interval_seconds=int(signal_config.calibrator_refresh_interval_seconds),
        recency_hours=int(signal_config.calibrator_recency_hours),
    )
    # 注入 per-TF 近期窗口配置
    if signal_config.calibrator_recency_hours_by_tf:
        calibrator.update_recency_config(
            hours_by_tf=signal_config.calibrator_recency_hours_by_tf,
        )
    performance_tracker = StrategyPerformanceTracker(
        config=build_performance_tracker_config(signal_config),
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
            cfg["name"] for cfg in signal_config.voting_group_configs if cfg.get("name")
        )
        if group_names:
            htf_source_strategies = group_names
    htf_cache = HTFStateCache(
        htf_map=htf_map if htf_map else None,
        max_age_seconds=signal_config.htf_cache_max_age_seconds,
        source_strategies=htf_source_strategies,
    )
    signal_module = SignalModule(
        indicator_source=UnifiedIndicatorSourceAdapter(indicator_manager),
        strategies=build_default_strategy_set(),
        repository=TimescaleSignalRepository(
            storage_writer.db, storage_writer=storage_writer
        ),
        calibrator=calibrator,
        performance_tracker=performance_tracker,
        soft_regime_enabled=signal_config.soft_regime_enabled,
        confidence_floor=signal_config.confidence_floor,
        confidence_floor_min_affinity=signal_config.confidence_floor_min_affinity,
        regime_detector=regime_detector,
    )

    # ── 应用配置化参数覆盖 ────────────────────────────────────────────
    _apply_strategy_config_overrides(signal_module, signal_config)

    # ── HTF 配置从策略声明自动推导（替代 INI [strategy_htf]）──────────
    _htf_target_config = signal_module.htf_target_config()
    if _htf_target_config:
        _factory_logger.info(
            "HTF target config (auto-derived from strategies): %s",
            _htf_target_config,
        )

    # 从策略的 preferred_scopes + required_indicators + htf_required_indicators
    # 自动推导指标计算集合，分别注入到 indicator_manager 的 confirmed 和 intrabar 路径。
    # 三源推导：策略自用指标 ∪ 策略 HTF 跨 TF 指标 ∪ 基础设施固定依赖
    # 基础设施固定依赖：regime(adx14/boll20/keltner20/rsi14) + filter(atr14/adx14/rsi14) + sizing(atr14)
    _INFRA_INDICATORS = frozenset({"atr14", "adx14", "rsi14", "boll20", "keltner20"})
    indicator_manager.set_confirmed_eligible_override(
        signal_module.confirmed_required_indicators()
        | signal_module.htf_required_indicators()
        | _INFRA_INDICATORS
    )
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
    # WAL-backed persistent queue for confirmed signal events
    from src.config import get_runtime_data_path

    wal_db_path = get_runtime_data_path("signal_queue.db")

    signal_runtime = SignalRuntime(
        service=signal_module,
        snapshot_source=indicator_manager,
        targets=runtime_targets,
        enable_confirmed_snapshot=True,
        policy=signal_policy,
        filter_chain=filter_chain,
        market_structure_analyzer=market_structure_analyzer,
        regime_detector=regime_detector,
        htf_indicators_enabled=signal_config.htf_indicators_enabled,
        intrabar_confidence_factor=signal_config.intrabar_confidence_factor,
        htf_context_fn=htf_cache.get_htf_context,
        htf_target_config=_htf_target_config,
        wal_db_path=wal_db_path,
    )
    signal_runtime.set_economic_calendar_service(economic_calendar_service)

    end_of_day_closeout = ExposureCloseoutController(
        ExposureCloseoutService(trade_module)
    )
    from src.trading.positions.exit_rules import ChandelierConfig as _ChandelierConfig
    from src.trading.positions.exit_rules import ExitProfile as _EP
    from src.trading.positions.exit_rules import profile_from_aggression as _pfa

    _chandelier_cfg = _ChandelierConfig(
        regime_aware=signal_config.chandelier_regime_aware,
        default_profile=_pfa(signal_config.chandelier_default_alpha),
        breakeven_enabled=signal_config.chandelier_breakeven_enabled,
        breakeven_buffer_r=signal_config.chandelier_breakeven_buffer_r,
        min_breakeven_buffer=signal_config.chandelier_min_breakeven_buffer,
        signal_exit_enabled=signal_config.chandelier_signal_exit_enabled,
        signal_exit_confirmation_bars=signal_config.chandelier_signal_exit_confirmation_bars,
        timeout_bars=signal_config.chandelier_timeout_bars,
        max_tp_r=signal_config.chandelier_max_tp_r,
        enforce_r_floor=signal_config.chandelier_enforce_r_floor,
        aggression_overrides=dict(signal_config.chandelier_aggression_overrides),
        tf_trail_scale=dict(signal_config.chandelier_tf_trail_scale),
    )
    position_manager = PositionManager(
        trading_module=trade_module,
        end_of_day_closeout=end_of_day_closeout,
        chandelier_config=_chandelier_cfg,
        indicator_source=indicator_manager,
        regime_detector=regime_detector,
        end_of_day_close_enabled=signal_config.end_of_day_close_enabled,
        end_of_day_close_hour_utc=signal_config.end_of_day_close_hour_utc,
        end_of_day_close_minute_utc=signal_config.end_of_day_close_minute_utc,
    )
    position_manager.set_sl_tp_history_writer(
        storage_writer.db.write_position_sl_tp_history
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
        cancellation_port=trade_module,
        execute_fn=lambda event, params, cost: (
            execute_market_order(_executor_holder[0], event, params, cost_metrics=cost)
            if _executor_holder
            else None
        ),
        on_expired_fn=lambda sid, reason: (
            _skip_callback_holder[0](sid, reason) if _skip_callback_holder else None
        ),
        inspect_mt5_order_fn=lambda info: (
            inspect_pending_mt5_order(_executor_holder[0], info)
            if _executor_holder
            else {"status": "pending"}
        ),
    )
    # 权益曲线过滤器
    from src.trading.execution.equity_filter import (
        EquityCurveFilter,
        EquityCurveFilterConfig,
    )

    equity_filter: EquityCurveFilter | None = None
    if signal_config.equity_curve_filter_enabled:
        equity_filter = EquityCurveFilter(
            balance_getter=lambda: _get_balance_for_equity_filter(trade_module),
            config=EquityCurveFilterConfig(
                enabled=True,
                ma_period=signal_config.equity_curve_filter_ma_period,
                min_samples=signal_config.equity_curve_filter_min_samples,
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
        pipeline_event_bus=pipeline_event_bus,
        equity_curve_filter=equity_filter,
    )
    _executor_holder.append(trade_executor)
    signal_runtime.add_signal_listener(trade_executor.on_signal_event)
    signal_runtime.add_signal_listener(position_manager.on_signal_event)
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

    # ── IntrabarTradeCoordinator + IntrabarTradeGuard ──
    # Intrabar trigger 路由由 Ingestor 管理（子 TF close → 合成父 TF bar）。
    # 这里只构建信号层的 coordinator（稳定性判定）和执行层的 guard（去重/协调）。
    from src.signals.orchestration.intrabar_trade_coordinator import (
        IntrabarTradeCoordinator,
        IntrabarTradingPolicy,
    )
    from src.trading.execution.intrabar_guard import IntrabarTradeGuard

    if (
        signal_config.intrabar_trading_enabled
        and signal_config.intrabar_trading_enabled_strategies
    ):
        intrabar_policy = IntrabarTradingPolicy(
            min_stable_bars=signal_config.intrabar_trading_min_stable_bars,
            min_confidence=signal_config.intrabar_trading_min_confidence,
            enabled_strategies=frozenset(
                signal_config.intrabar_trading_enabled_strategies
            ),
        )
        intrabar_coordinator = IntrabarTradeCoordinator(policy=intrabar_policy)
        signal_runtime.set_intrabar_trade_coordinator(intrabar_coordinator)
        intrabar_guard = IntrabarTradeGuard()
        trade_executor.set_intrabar_guard(intrabar_guard)
        # 注册 intrabar 交易信号 listener
        signal_runtime.add_signal_listener(trade_executor.on_intrabar_trade_signal)
        _factory_logger.info(
            "Intrabar trading enabled: strategies=%s, min_stable_bars=%d, "
            "min_confidence=%.2f",
            sorted(intrabar_policy.enabled_strategies),
            intrabar_policy.min_stable_bars,
            intrabar_policy.min_confidence,
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
        regime_detector=regime_detector,
        market_structure_analyzer=market_structure_analyzer,
        signal_module=signal_module,
        signal_runtime=signal_runtime,
        htf_cache=htf_cache,
        signal_quality_tracker=signal_quality_tracker,
        trade_outcome_tracker=trade_outcome_tracker,
        exposure_closeout_controller=end_of_day_closeout,
        position_manager=position_manager,
        trade_executor=trade_executor,
        performance_tracker=performance_tracker,
        pending_entry_manager=pending_entry_manager,
    )


def _get_balance_for_equity_filter(trade_module: Any) -> float | None:
    try:
        info = trade_module.account_info()
        if isinstance(info, dict):
            return float(info.get("equity") or info.get("balance") or 0)
        return float(
            getattr(info, "equity", None) or getattr(info, "balance", None) or 0
        )
    except Exception:
        return None


def _apply_strategy_hot_reload(signal_module: Any, signal_config: Any) -> None:
    """热更新策略参数 + Regime 亲和度。"""
    try:
        signal_module.apply_param_overrides(
            signal_config.strategy_params,
            signal_config.regime_affinity_overrides or None,
            strategy_params_per_tf=signal_config.strategy_params_per_tf or None,
        )
        _factory_logger.info("Hot reload: strategy params + regime affinity updated")
    except Exception:
        _factory_logger.exception(
            "Hot reload: failed to apply strategy param overrides"
        )


def _apply_regime_detector_hot_reload(regime_detector: Any, signal_config: Any) -> None:
    """热更新 Regime 检测器阈值。"""
    try:
        regime_detector.update_thresholds(
            adx_trending=getattr(signal_config, "regime_adx_trending_threshold", None),
            adx_ranging=getattr(signal_config, "regime_adx_ranging_threshold", None),
            bb_tight_pct=getattr(signal_config, "regime_bb_tight_pct", None),
        )
        _factory_logger.info("Hot reload: regime detector thresholds updated")
    except Exception:
        _factory_logger.exception("Hot reload: failed to update regime detector")


def _apply_calibrator_hot_reload(calibrator: Any, signal_config: Any) -> None:
    """热更新 Calibrator 参数。"""
    try:
        calibrator.update_recency_config(
            recency_hours=int(signal_config.calibrator_recency_hours),
            hours_by_tf=(
                signal_config.calibrator_recency_hours_by_tf
                if signal_config.calibrator_recency_hours_by_tf
                else None
            ),
        )
        _factory_logger.info("Hot reload: calibrator config updated")
    except Exception:
        _factory_logger.exception("Hot reload: failed to update calibrator")


def register_signal_hot_reload(
    signal_runtime,
    signal_config_loader,
    *,
    signal_module=None,
    regime_detector=None,
    trade_executor=None,
    economic_calendar_service=None,
    market_structure_analyzer=None,
    performance_tracker=None,
    pending_entry_manager=None,
    calibrator=None,
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
        # 策略参数 + Regime 亲和度热更新
        if signal_module is not None:
            _apply_strategy_hot_reload(signal_module, signal_config)
        # Regime 检测器参数热更新
        if regime_detector is not None:
            _apply_regime_detector_hot_reload(regime_detector, signal_config)
        # Calibrator per-TF 窗口热更新
        if calibrator is not None:
            _apply_calibrator_hot_reload(calibrator, signal_config)
        if trade_executor is not None:
            trade_executor.config = build_executor_config(signal_config)
            trade_executor.update_execution_gate_config(
                build_execution_gate_config(signal_config)
            )
        if performance_tracker is not None:
            performance_tracker.update_config(
                build_performance_tracker_config(signal_config)
            )
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
        _factory_logger.info("signal.ini hot reload complete")

    manager = get_file_config_manager()
    manager.register_change_callback(_on_signal_config_change)

    def _cleanup() -> None:
        manager.unregister_change_callback(_on_signal_config_change)

    return _cleanup
