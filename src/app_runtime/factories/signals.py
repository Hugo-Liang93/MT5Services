from __future__ import annotations

import logging as _logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from src.config.models.signal import SignalConfig

from src.app_runtime.factories.admission_writeback import (
    make_intent_published_listener,
    make_skip_listener,
    multicast,
)
from src.calendar import EconomicDecayService
from src.calendar.policy import build_signal_economic_policy
from src.config import get_economic_config
from src.config.file_manager import get_file_config_manager
from src.config.mt5 import load_group_mt5_settings
from src.market_structure import MarketStructureAnalyzer, MarketStructureConfig
from src.signals.contracts import (
    StrategyDeployment,
    StrategyDeploymentStatus,
    normalize_session_name,
    validate_strategy_deployments,
)
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
from src.signals.orchestration.policy import SignalPolicy
from src.signals.orchestration.runtime import SignalRuntime, SignalTarget
from src.signals.service import SignalModule
from src.signals.strategies.adapters import UnifiedIndicatorSourceAdapter
from src.signals.strategies.catalog import build_default_strategy_set
from src.signals.strategies.htf_cache import HTFStateCache
from src.signals.tracking.repository import TimescaleSignalRepository
from src.trading.closeout import ExposureCloseoutController, ExposureCloseoutService
from src.trading.execution.eventing import execute_market_order
from src.trading.execution.executor import ExecutorConfig, TradeExecutor
from src.trading.execution.gate import ExecutionGate, ExecutionGateConfig
from src.trading.execution.pending_orders import inspect_pending_mt5_order
from src.trading.execution.quote_health import build_execution_quote_health
from src.trading.execution.risk_caps import MaxSingleTradeLossPolicy
from src.trading.execution.sizing import RegimeSizing
from src.trading.intents import ExecutionIntentConsumer, ExecutionIntentPublisher
from src.trading.pending import PendingEntryConfig, PendingEntryManager
from src.trading.positions import ConfirmedIndicatorSource, PositionManager
from src.trading.tracking import SignalQualityTracker, TradeOutcomeTracker

_factory_logger = _logging.getLogger(__name__)


def _filter_strategies_for_environment(
    strategies: list[Any],
    deployments: Mapping[str, StrategyDeployment],
    environment: str | None,
) -> list[Any]:
    """按 instance environment 过滤要装配的策略集合。

    装配规则（参考 ADR-010 / docs/superpowers/specs）：
      - environment="live"    → 仅 ACTIVE + ACTIVE_GUARDED（实盘交易策略）
      - environment="demo"    → ACTIVE + ACTIVE_GUARDED + DEMO_VALIDATION（含演练候选）
      - environment 未知/None → 全量保留（向后兼容；记 WARNING）

    没有 deployment 契约的策略保留——交由下游 _validate_strategy_deployment_contracts
    报错，避免在此处吞掉契约缺失。
    """
    if environment not in {"live", "demo"}:
        _factory_logger.warning(
            "build_signal_components: environment=%r 未知；装配所有策略不按 deployment status 过滤",
            environment,
        )
        return list(strategies)

    filtered: list[Any] = []
    skipped: list[str] = []
    for strategy in strategies:
        deployment = deployments.get(strategy.name)
        if deployment is None:
            filtered.append(strategy)
            continue
        if environment == "live":
            allowed = deployment.allows_live_execution()
        else:  # environment == "demo"
            allowed = deployment.allows_demo_validation()
        if allowed:
            filtered.append(strategy)
        else:
            skipped.append(strategy.name)
    if skipped:
        _factory_logger.info(
            "[%s] excluded %d strategies by deployment status: %s",
            environment,
            len(skipped),
            ", ".join(sorted(skipped)),
        )
    return filtered


def _apply_strategy_config_overrides(module: SignalModule, signal_config) -> None:
    """从 signal_config 构建 TFParamResolver 并注入到各策略 + 应用 regime_affinity 覆盖。"""
    from src.signals.evaluation.regime import RegimeType
    from src.signals.strategies.tf_params import build_tf_param_resolver

    strategies = module.strategies

    # ── 构建 per-TF 参数查表器并注入 ─────────────────────────────────
    resolver = build_tf_param_resolver(
        signal_config.strategy_params,
        signal_config.strategy_params_per_tf,
    )
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
    economic_decay_service: EconomicDecayService
    trade_outcome_tracker: TradeOutcomeTracker | None
    exposure_closeout_controller: ExposureCloseoutController | None
    position_manager: PositionManager | None
    trade_executor: TradeExecutor | None
    performance_tracker: StrategyPerformanceTracker
    signal_performance_tracker: StrategyPerformanceTracker
    execution_performance_tracker: StrategyPerformanceTracker
    pending_entry_manager: PendingEntryManager | None
    execution_intent_publisher: ExecutionIntentPublisher | None
    execution_intent_consumer: ExecutionIntentConsumer | None


@dataclass(frozen=True)
class AccountRuntimeComponents:
    trade_outcome_tracker: TradeOutcomeTracker
    exposure_closeout_controller: ExposureCloseoutController
    position_manager: PositionManager
    trade_executor: TradeExecutor
    pending_entry_manager: PendingEntryManager
    execution_intent_consumer: ExecutionIntentConsumer | None


def build_pending_entry_config(signal_config) -> PendingEntryConfig:
    return PendingEntryConfig(
        check_interval=signal_config.pending_entry_check_interval,
        max_spread_points=signal_config.pending_entry_max_spread_points,
        timeout_bars=dict(signal_config.pending_entry_timeout_bars),
        default_timeout_bars=signal_config.pending_entry_default_timeout_bars,
        cancel_on_new_signal=signal_config.pending_entry_cancel_on_new_signal,
        cancel_same_direction=signal_config.pending_entry_cancel_same_direction,
    )


def build_economic_decay_service(
    economic_calendar_service,
    economic_config=None,
) -> EconomicDecayService:
    """Build the EconomicDecayService used by both filter and decay queries.

    Returned instance is shared between the SignalFilterChain (hard-block)
    and SignalRuntime evaluator (confidence decay) so both surfaces query
    the exact same window/importance/symbol-context.
    """
    policy = build_signal_economic_policy(economic_config or get_economic_config())
    return EconomicDecayService(
        provider=economic_calendar_service,
        policy=policy,
    )


def build_signal_filter_chain(
    signal_config,
    economic_decay_service: EconomicDecayService,
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
            service=economic_decay_service,
        ),
        volatility_filter=VolatilitySpikeFilter(
            spike_multiplier=signal_config.volatility_atr_spike_multiplier,
        ),
        trend_exhaustion_filter=TrendExhaustionFilter(),
    )


def build_executor_config(signal_config: "SignalConfig") -> ExecutorConfig:
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
        timeframe_min_confidence=dict(signal_config.timeframe_min_confidence),
        htf_conflict_block_timeframes=frozenset(
            signal_config.htf_conflict_block_timeframes
        ),
        htf_conflict_exempt_categories=frozenset(
            signal_config.htf_conflict_exempt_categories
        ),
        max_consecutive_failures=signal_config.max_consecutive_failures,
        circuit_auto_reset_minutes=signal_config.circuit_auto_reset_minutes,
        max_spread_to_stop_ratio=signal_config.max_spread_to_stop_ratio,
        reentry_cooldown_bars=signal_config.reentry_cooldown_bars,
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
        strategy_deployments=dict(signal_config.strategy_deployments),
        max_single_trade_loss_policy=MaxSingleTradeLossPolicy(
            max_loss_usd=signal_config.max_single_trade_loss_usd,
        ),
    )


def build_execution_gate_config(signal_config: "SignalConfig") -> ExecutionGateConfig:
    return ExecutionGateConfig(
        intrabar_trading_enabled=signal_config.intrabar_trading_enabled,
        intrabar_enabled_strategies=frozenset(
            s for s in signal_config.intrabar_trading_enabled_strategies if s
        ),
    )


def build_signal_policy(signal_config: "SignalConfig") -> SignalPolicy:
    allowed_sessions = tuple(
        normalize_session_name(session)
        for session in signal_config.allowed_sessions.split(",")
        if session.strip()
    )
    strategy_deployments = dict(signal_config.strategy_deployments)
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
    for strategy_name, deployment in strategy_deployments.items():
        if deployment.locked_sessions:
            strategy_sessions[strategy_name] = tuple(deployment.locked_sessions)
        if deployment.locked_timeframes:
            strategy_timeframes[strategy_name] = tuple(deployment.locked_timeframes)
    return SignalPolicy(
        snapshot_dedupe_window_seconds=signal_config.snapshot_dedupe_window_seconds,
        max_spread_points=signal_config.max_spread_points,
        allowed_sessions=allowed_sessions,
        strategy_sessions=strategy_sessions,
        strategy_timeframes=strategy_timeframes,
        strategy_deployments=strategy_deployments,
    )


def _validate_strategy_deployment_contracts(
    signal_config: "SignalConfig",
    strategies: Mapping[str, Any],
) -> dict[str, StrategyDeployment]:
    deployments = validate_strategy_deployments(
        deployments=dict(signal_config.strategy_deployments),
        known_strategies=tuple(strategies.keys()),
        strategy_timeframes_policy=dict(signal_config.strategy_timeframes),
        strategy_sessions_policy=dict(signal_config.strategy_sessions),
        regime_affinity_overrides=dict(signal_config.regime_affinity_overrides),
    )
    missing_contracts = sorted(
        strategy_name
        for strategy_name in strategies
        if strategy_name not in deployments
    )
    if missing_contracts:
        raise ValueError(
            "Explicit strategy deployment contracts are required for all registered "
            "strategies; missing: " + ", ".join(missing_contracts)
        )
    return deployments


def _validate_execution_contracts(
    *,
    signal_config: Any,
    deployments: Mapping[str, StrategyDeployment],
    runtime_identity: Any | None,
) -> None:
    if runtime_identity is None or not signal_config.auto_trade_enabled:
        return

    live_executable_strategies = sorted(
        strategy_name
        for strategy_name, deployment in deployments.items()
        if deployment.allows_live_execution()
    )
    if not live_executable_strategies:
        return

    configured_accounts = load_group_mt5_settings(
        instance_name=runtime_identity.instance_name,
    )
    normalized_bindings = _normalized_account_bindings(signal_config)

    unknown_aliases = sorted(
        alias for alias in normalized_bindings if alias not in configured_accounts
    )
    if unknown_aliases:
        raise ValueError(
            "account_bindings reference unconfigured MT5 accounts: "
            + ", ".join(unknown_aliases)
        )

    bound_live_strategies = {
        strategy_name
        for strategies in normalized_bindings.values()
        for strategy_name in strategies
    }
    unbound_live_strategies = sorted(
        strategy_name
        for strategy_name in live_executable_strategies
        if strategy_name not in bound_live_strategies
    )
    if unbound_live_strategies:
        raise ValueError(
            "auto_trade_enabled requires explicit account_bindings for every "
            "live-executable strategy; missing bindings for: "
            + ", ".join(unbound_live_strategies)
        )


def _normalized_account_bindings(
    signal_config: "SignalConfig",
) -> dict[str, set[str]]:
    return {
        str(alias).strip(): {
            str(strategy).strip()
            for strategy in (strategies or [])
            if str(strategy).strip()
        }
        for alias, strategies in signal_config.account_bindings.items()
        if str(alias).strip()
    }


def _should_attach_local_account_runtime(
    *,
    signal_config: Any,
    deployments: Mapping[str, StrategyDeployment],
    runtime_identity: Any,
) -> bool:
    # §0dk P2：runtime_identity 必填（装配契约保证），直接 access 不 getattr 兜底。
    if runtime_identity is None:
        raise ValueError(
            "_should_attach_local_account_runtime requires runtime_identity"
        )

    if runtime_identity.instance_role != "main":
        return True

    if runtime_identity.live_topology_mode != "multi_account":
        return True

    account_alias = str(runtime_identity.account_alias or "").strip()
    if not account_alias:
        return False

    bound_strategies = _normalized_account_bindings(signal_config).get(
        account_alias, set()
    )
    if not bound_strategies:
        return False

    return any(
        deployment is not None and deployment.allows_live_execution()
        for deployment in (
            deployments.get(strategy_name) for strategy_name in bound_strategies
        )
    )


def build_account_runtime_components(
    *,
    market_service,
    storage_writer,
    trade_module,
    signal_config,
    execution_performance_tracker,
    runtime_identity=None,
    trading_state_store=None,
    pipeline_event_bus=None,
    regime_detector=None,
    on_execution_skip: Callable[[str, str], None] | None = None,
) -> AccountRuntimeComponents:
    end_of_day_closeout = ExposureCloseoutController(
        ExposureCloseoutService(trade_module)
    )
    # ChandelierConfig 构建职责归 exit_rules 模块（出场参数域）；
    # signals 工厂只负责把 signal_config 喂进单一构建入口。
    from src.trading.positions.exit_rules import build_chandelier_config

    _chandelier_cfg = build_chandelier_config(signal_config)
    position_manager = PositionManager(
        trading_module=trade_module,
        end_of_day_closeout=end_of_day_closeout,
        chandelier_config=_chandelier_cfg,
        indicator_source=ConfirmedIndicatorSource(market_service),
        regime_detector=regime_detector,
        end_of_day_close_enabled=signal_config.end_of_day_close_enabled,
        end_of_day_close_hour_utc=signal_config.end_of_day_close_hour_utc,
        end_of_day_close_minute_utc=signal_config.end_of_day_close_minute_utc,
    )

    def _write_position_sl_tp_history(rows: list[tuple]) -> None:
        account_alias = (
            runtime_identity.account_alias if runtime_identity is not None else ""
        )
        account_key = (
            runtime_identity.account_key if runtime_identity is not None else None
        )
        normalized_rows = []
        for row in rows:
            normalized_rows.append(
                (
                    row[0],
                    account_alias,
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                    row[9],
                    row[10],
                    row[11],
                    row[12],
                    row[13],
                    row[14],
                    row[15],
                    row[16],
                    row[17],
                    row[18],
                    account_key,
                )
            )

        storage_writer.db.write_position_sl_tp_history(normalized_rows)

    position_manager.set_sl_tp_history_writer(_write_position_sl_tp_history)

    trade_outcome_tracker = TradeOutcomeTracker(
        write_fn=storage_writer.db.write_trade_outcomes,
        on_outcome_fn=execution_performance_tracker.record_outcome,
    )

    persist_execution_fn = getattr(storage_writer.db, "write_auto_executions", None)
    execution_gate = ExecutionGate(config=build_execution_gate_config(signal_config))
    _executor_holder: list[TradeExecutor] = []
    pending_entry_manager = PendingEntryManager(
        config=build_pending_entry_config(signal_config),
        market_service=market_service,
        cancellation_port=trade_module,
        execute_fn=lambda event, params, cost: (
            execute_market_order(_executor_holder[0], event, params, cost_metrics=cost)
            if _executor_holder
            else None
        ),
        on_expired_fn=on_execution_skip,
        inspect_mt5_order_fn=lambda info: (
            inspect_pending_mt5_order(_executor_holder[0], info)
            if _executor_holder
            else {"status": "pending"}
        ),
    )

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
        circuit_breaker_history_fn=storage_writer.db.write_circuit_breaker_history,
        on_execution_skip=on_execution_skip,
        execution_gate=execution_gate,
        pending_entry_manager=pending_entry_manager,
        performance_tracker=execution_performance_tracker,
        pipeline_event_bus=pipeline_event_bus,
        equity_curve_filter=equity_filter,
        quote_health_fn=lambda symbol: build_execution_quote_health(
            market_service,
            symbol,
        ),
        runtime_identity=runtime_identity,
    )
    _executor_holder.append(trade_executor)

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

    execution_intent_consumer = None
    if runtime_identity is not None:
        execution_intent_consumer = ExecutionIntentConsumer(
            claim_fn=storage_writer.db.claim_execution_intents,
            complete_fn=storage_writer.db.complete_execution_intent,
            heartbeat_fn=storage_writer.db.heartbeat_execution_intent,
            runtime_identity=runtime_identity,
            trade_executor=trade_executor,
            pipeline_event_bus=pipeline_event_bus,
        )

    return AccountRuntimeComponents(
        trade_outcome_tracker=trade_outcome_tracker,
        exposure_closeout_controller=end_of_day_closeout,
        position_manager=position_manager,
        trade_executor=trade_executor,
        pending_entry_manager=pending_entry_manager,
        execution_intent_consumer=execution_intent_consumer,
    )


def build_signal_components(
    *,
    indicator_manager,
    storage_writer,
    trade_module,
    economic_calendar_service,
    signal_config,
    runtime_identity=None,
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
    signal_performance_tracker = StrategyPerformanceTracker(
        config=build_performance_tracker_config(signal_config),
    )
    execution_performance_tracker = StrategyPerformanceTracker(
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
    htf_cache = HTFStateCache(
        htf_map=htf_map if htf_map else None,
        max_age_seconds=signal_config.htf_cache_max_age_seconds,
    )

    # ── Environment-aware 策略装配过滤（ADR-010）──────────────────────
    # 装配集合：live = ACTIVE ∪ ACTIVE_GUARDED；demo = live ∪ DEMO_VALIDATION
    # 注：deployments 同步过滤后写回 signal_config，确保下游 validate / runtime
    #     / pre_trade_checks 都看到一致的策略集合。
    _instance_environment = (
        runtime_identity.environment if runtime_identity is not None else None
    )
    _all_strategies = build_default_strategy_set()
    _all_deployments = dict(signal_config.strategy_deployments)
    _filtered_strategies = _filter_strategies_for_environment(
        _all_strategies, _all_deployments, _instance_environment
    )
    _filtered_strategy_names = {s.name for s in _filtered_strategies}
    _filtered_deployments = {
        name: dep
        for name, dep in _all_deployments.items()
        if name in _filtered_strategy_names
    }
    signal_config.strategy_deployments = _filtered_deployments
    _factory_logger.info(
        "[%s] signal layer assembling %d strategies (filtered from %d)",
        _instance_environment or "unknown",
        len(_filtered_strategies),
        len(_all_strategies),
    )

    signal_module = SignalModule(
        indicator_source=UnifiedIndicatorSourceAdapter(indicator_manager),
        strategies=_filtered_strategies,
        repository=TimescaleSignalRepository(
            storage_writer.db, storage_writer=storage_writer
        ),
        calibrator=calibrator,
        performance_tracker=signal_performance_tracker,
        soft_regime_enabled=signal_config.soft_regime_enabled,
        confidence_floor=signal_config.confidence_floor,
        confidence_floor_min_affinity=signal_config.confidence_floor_min_affinity,
        regime_detector=regime_detector,
    )

    # ── 应用配置化参数覆盖 ────────────────────────────────────────────
    _apply_strategy_config_overrides(signal_module, signal_config)
    # P10.2: 注入 account_bindings 供 Intel/Cockpit 读模型反向索引使用
    signal_module.set_account_bindings(signal_config.account_bindings)
    validated_deployments = _validate_strategy_deployment_contracts(
        signal_config,
        signal_module.strategies,
    )
    signal_config.strategy_deployments = validated_deployments
    _validate_execution_contracts(
        signal_config=signal_config,
        deployments=validated_deployments,
        runtime_identity=runtime_identity,
    )
    attach_local_account_runtime = _should_attach_local_account_runtime(
        signal_config=signal_config,
        deployments=validated_deployments,
        runtime_identity=runtime_identity,
    )

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
    economic_decay_service = build_economic_decay_service(economic_calendar_service)
    filter_chain = build_signal_filter_chain(signal_config, economic_decay_service)
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
    signal_runtime.set_economic_decay_service(economic_decay_service)
    _skip_callback_holder: list[Callable[[str, str], None]] = []
    trade_outcome_tracker = None
    end_of_day_closeout = None
    position_manager = None
    trade_executor = None
    pending_entry_manager = None
    execution_intent_consumer = None
    if attach_local_account_runtime:
        account_runtime = build_account_runtime_components(
            market_service=indicator_manager.market_service,
            storage_writer=storage_writer,
            trade_module=trade_module,
            signal_config=signal_config,
            execution_performance_tracker=execution_performance_tracker,
            runtime_identity=runtime_identity,
            trading_state_store=trading_state_store,
            pipeline_event_bus=pipeline_event_bus,
            regime_detector=regime_detector,
            on_execution_skip=lambda sid, reason: (
                _skip_callback_holder[0](sid, reason) if _skip_callback_holder else None
            ),
        )
        trade_outcome_tracker = account_runtime.trade_outcome_tracker
        end_of_day_closeout = account_runtime.exposure_closeout_controller
        position_manager = account_runtime.position_manager
        trade_executor = account_runtime.trade_executor
        pending_entry_manager = account_runtime.pending_entry_manager
        execution_intent_consumer = account_runtime.execution_intent_consumer
        signal_runtime.add_signal_listener(position_manager.on_signal_event)
    else:
        # §0dk P2：runtime_identity 必填，无字面量 "shared-main" 兜底。
        _factory_logger.info(
            "Signal runtime %s will publish intents only; local account runtime disabled",
            runtime_identity.instance_id,
        )
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
    if position_manager is not None and trade_outcome_tracker is not None:
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
    if trading_state_store is not None and pending_entry_manager is not None:
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
    if trading_state_store is not None and position_manager is not None:
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
        if trade_executor is not None:
            intrabar_guard = IntrabarTradeGuard()
            trade_executor.set_intrabar_guard(intrabar_guard)
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
        on_quality_fn=signal_performance_tracker.record_outcome,
    )
    signal_quality_tracker.attach(signal_runtime)
    # 绑定延迟引用：TradeExecutor skip → multicast(SignalQualityTracker, AdmissionWriteback)
    # P9 Phase 1.5: admission writeback listener 与 quality tracker 并行触发，
    # 任一失败不影响另一方（multicast 内部 try/except）。
    _admission_skip_listener = make_skip_listener(signal_module.repository)
    _skip_callback_holder.append(
        multicast(signal_quality_tracker.on_execution_skip, _admission_skip_listener)
    )

    # P9 Phase 1.5: 接受路径回填 — 监听 PipelineEventBus 所有事件，
    # listener 内部按 event.type == "intent_published" 过滤（add_listener 接口语义）。
    # publisher.py:172-194 emit 该事件，payload.signal_id 必填。
    if pipeline_event_bus is not None:
        pipeline_event_bus.add_listener(
            make_intent_published_listener(signal_module.repository)
        )

    execution_intent_publisher = None
    if runtime_identity is not None:
        execution_intent_publisher = ExecutionIntentPublisher(
            write_fn=storage_writer.db.write_execution_intents,
            runtime_identity=runtime_identity,
            account_bindings=dict(signal_config.account_bindings),
            strategy_deployments=dict(validated_deployments),
            auto_trade_enabled=signal_config.auto_trade_enabled,
            pipeline_event_bus=pipeline_event_bus,
        )
        signal_runtime.add_signal_listener(execution_intent_publisher.on_signal_event)

    return SignalComponents(
        calibrator=calibrator,
        regime_detector=regime_detector,
        market_structure_analyzer=market_structure_analyzer,
        signal_module=signal_module,
        signal_runtime=signal_runtime,
        htf_cache=htf_cache,
        signal_quality_tracker=signal_quality_tracker,
        economic_decay_service=economic_decay_service,
        trade_outcome_tracker=trade_outcome_tracker,
        exposure_closeout_controller=end_of_day_closeout,
        position_manager=position_manager,
        trade_executor=trade_executor,
        performance_tracker=signal_performance_tracker,
        signal_performance_tracker=signal_performance_tracker,
        execution_performance_tracker=execution_performance_tracker,
        pending_entry_manager=pending_entry_manager,
        execution_intent_publisher=execution_intent_publisher,
        execution_intent_consumer=execution_intent_consumer,
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


def _apply_regime_detector_hot_reload(
    regime_detector: Any, signal_config: "SignalConfig"
) -> None:
    """热更新 Regime 检测器阈值。"""
    try:
        regime_detector.update_thresholds(
            adx_trending=signal_config.regime_adx_trending_threshold,
            adx_ranging=signal_config.regime_adx_ranging_threshold,
            bb_tight_pct=signal_config.regime_bb_tight_pct,
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
    economic_config_loader=None,
    runtime_timeframes=None,
    signal_module=None,
    regime_detector=None,
    trade_executor=None,
    economic_calendar_service=None,
    market_structure_analyzer=None,
    performance_tracker=None,
    signal_performance_tracker=None,
    execution_performance_tracker=None,
    execution_intent_publisher=None,
    pending_entry_manager=None,
    calibrator=None,
) -> Callable[[], None]:
    if economic_config_loader is None:
        economic_config_loader = get_economic_config

    def _on_signal_config_change(filename: str) -> None:
        if filename not in {"signal.ini", "economic.ini"}:
            return
        signal_config = signal_config_loader()
        economic_config = economic_config_loader()
        if (
            filename == "signal.ini"
            and signal_module is not None
            and runtime_timeframes is not None
        ):
            from src.app_runtime.builder_phases.signal import (
                _validate_intrabar_trigger_coverage,
            )

            _validate_intrabar_trigger_coverage(
                signal_module,
                signal_config,
                effective_timeframes=tuple(runtime_timeframes),
            )
        if signal_runtime is not None:
            signal_runtime.update_policy(build_signal_policy(signal_config))
            new_decay_service = build_economic_decay_service(
                economic_calendar_service, economic_config
            )
            signal_runtime.filter_chain = build_signal_filter_chain(
                signal_config, new_decay_service
            )
            signal_runtime.set_economic_decay_service(new_decay_service)
        if filename == "economic.ini":
            _factory_logger.info("economic.ini hot reload complete")
            return
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
        trackers = [
            tracker
            for tracker in (
                signal_performance_tracker,
                execution_performance_tracker,
                performance_tracker,
            )
            if tracker is not None
        ]
        for tracker in trackers:
            tracker.update_config(build_performance_tracker_config(signal_config))
        if execution_intent_publisher is not None:
            execution_intent_publisher.update_bindings(
                account_bindings=dict(signal_config.account_bindings),
                strategy_deployments=dict(signal_config.strategy_deployments),
                auto_trade_enabled=signal_config.auto_trade_enabled,
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
