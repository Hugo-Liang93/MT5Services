from __future__ import annotations

from dataclasses import dataclass

from src.config.file_manager import get_file_config_manager
from src.market_structure import MarketStructureAnalyzer, MarketStructureConfig
from src.signals.contracts import normalize_session_name
from src.signals.execution.filters import (
    EconomicEventFilter,
    SessionFilter,
    SessionTransitionFilter,
    SignalFilterChain,
    SpreadFilter,
)
from src.signals.evaluation.calibrator import ConfidenceCalibrator
from src.signals.evaluation.performance import (
    PerformanceTrackerConfig,
    StrategyPerformanceTracker,
)

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
from src.trading.signal_executor import ExecutorConfig, TradeExecutor


def build_performance_tracker_config(signal_config) -> PerformanceTrackerConfig:
    return PerformanceTrackerConfig(
        enabled=signal_config.perf_tracker_enabled,
        baseline_win_rate=signal_config.perf_tracker_baseline_win_rate,
        min_multiplier=signal_config.perf_tracker_min_multiplier,
        max_multiplier=signal_config.perf_tracker_max_multiplier,
        streak_penalty_threshold=signal_config.perf_tracker_streak_penalty_threshold,
        streak_penalty_factor=signal_config.perf_tracker_streak_penalty_factor,
        category_fallback_min_samples=signal_config.perf_tracker_category_fallback_min_samples,
        session_reset_interval_hours=signal_config.perf_tracker_session_reset_interval_hours,
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
    )


def build_executor_config(signal_config) -> ExecutorConfig:
    # 汇总所有 voting group 成员策略（用于分组保护）
    voting_group_strategies: frozenset[str] = frozenset(
        strategy
        for cfg in signal_config.voting_group_configs
        for strategy in cfg.get("strategies", [])
    )
    return ExecutorConfig(
        enabled=signal_config.auto_trade_enabled,
        min_confidence=signal_config.auto_trade_min_confidence,
        require_armed=signal_config.auto_trade_require_armed,
        max_concurrent_positions_per_symbol=signal_config.max_concurrent_positions_per_symbol,
        risk_percent=signal_config.risk_percent_per_trade,
        sl_atr_multiplier=signal_config.sl_atr_multiplier,
        tp_atr_multiplier=signal_config.tp_atr_multiplier,
        min_volume=signal_config.min_volume,
        max_volume=signal_config.max_volume,
        contract_size_map=dict(signal_config.contract_size_map),
        max_consecutive_failures=signal_config.max_consecutive_failures,
        circuit_auto_reset_minutes=signal_config.circuit_auto_reset_minutes,
        max_spread_to_stop_ratio=signal_config.max_spread_to_stop_ratio,
        trade_trigger_strategies=tuple(signal_config.trade_trigger_strategies),
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
        auto_trade_enabled=signal_config.auto_trade_enabled,
        auto_trade_min_confidence=signal_config.auto_trade_min_confidence,
        auto_trade_require_armed=signal_config.auto_trade_require_armed,
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
    calibrator = ConfidenceCalibrator(
        fetch_winrates_fn=storage_writer.db.fetch_winrates,
        alpha=0.15,
        baseline_win_rate=0.50,
        max_boost=1.30,
        min_samples=50,
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
    )

    # 根据当前实际配置的时间框架决定 LTF→HTF 映射。
    # 系统当前只配置了 M1 和 H1，默认映射 M1→M5 会导致 M5 永远没有缓存数据，
    # MultiTimeframeConfirmStrategy 的 HTF 方向始终为 None（策略退化为 hold）。
    # 通过检测已配置时间框架来自动确定最近的 HTF，避免策略空转。
    configured_tfs = set(indicator_manager.config.timeframes)
    _default_htf_chain = ["M5", "M15", "H1", "H4", "D1"]
    m1_htf = next(
        (tf for tf in _default_htf_chain if tf in configured_tfs and tf != "M1"),
        None,
    )
    htf_map = {"M1": m1_htf} if m1_htf else {}
    htf_cache = HTFStateCache(htf_map=htf_map if htf_map else None)
    register_all_strategies(signal_module, htf_cache)
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
    )

    position_manager = PositionManager(
        trading_module=trade_module,
        trailing_atr_multiplier=signal_config.trailing_atr_multiplier,
        breakeven_atr_threshold=signal_config.breakeven_atr_threshold,
        end_of_day_close_enabled=signal_config.end_of_day_close_enabled,
        end_of_day_close_hour_utc=signal_config.end_of_day_close_hour_utc,
        end_of_day_close_minute_utc=signal_config.end_of_day_close_minute_utc,
    )
    # 交易结果追踪器：追踪实际执行的交易盈亏（由 TradeExecutor 登记，PositionManager 关仓评估）
    trade_outcome_tracker = TradeOutcomeTracker(
        write_fn=storage_writer.db.write_trade_outcomes,
        on_outcome_fn=performance_tracker.record_outcome,
    )

    persist_execution_fn = getattr(storage_writer.db, "write_auto_executions", None)
    trade_executor = TradeExecutor(
        trading_module=trade_module,
        config=build_executor_config(signal_config),
        position_manager=position_manager,
        htf_cache=htf_cache,
        persist_execution_fn=persist_execution_fn,
        trade_outcome_tracker=trade_outcome_tracker,
    )
    signal_runtime.add_signal_listener(trade_executor.on_signal_event)
    htf_cache.attach(signal_runtime)

    # 接线：PositionManager 关仓时通知 TradeOutcomeTracker
    position_manager.add_close_callback(trade_outcome_tracker.on_position_closed)

    # 信号质量追踪器：N bars 后评估信号预测质量（供 Calibrator 长期统计校准）
    signal_quality_tracker = SignalQualityTracker(
        write_fn=storage_writer.db.write_outcome_events,
        bars_to_evaluate=5,
        on_quality_fn=performance_tracker.record_outcome,
    )
    signal_quality_tracker.attach(signal_runtime)

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
    )


def register_signal_hot_reload(
    signal_runtime,
    signal_config_loader,
    *,
    trade_executor=None,
    economic_calendar_service=None,
    market_structure_analyzer=None,
    performance_tracker=None,
) -> None:
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
        if performance_tracker is not None:
            performance_tracker._config = build_performance_tracker_config(signal_config)
        if market_structure_analyzer is not None:
            market_structure_analyzer.config = MarketStructureConfig(
                enabled=signal_config.market_structure_enabled,
                lookback_bars=signal_config.market_structure_lookback_bars,
                m1_lookback_bars=signal_config.market_structure_m1_lookback_bars,
                open_range_minutes=signal_config.market_structure_open_range_minutes,
                compression_window_bars=signal_config.market_structure_compression_window_bars,
                compression_reference_bars=signal_config.market_structure_reference_window_bars,
            )

    get_file_config_manager().register_change_callback(_on_signal_config_change)
