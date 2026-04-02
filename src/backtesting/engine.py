"""回测引擎核心：逐 bar 回放历史数据，复用生产指标和策略组件。

设计原则：回测使用实盘方法，不重新实现，避免模拟失真。
- 过滤器：直接复用 SignalFilterChain（via BacktestFilterSimulator）
- 策略评估：直接复用 SignalModule.evaluate()（含完整置信度管线）
- 置信度后处理：复用 src/signals/confidence 中的纯函数
- 持仓管理：复用 src/trading/position_rules 中的纯函数
- 指标计算：直接复用 OptimizedPipeline.compute()
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from src.clients.mt5_market import OHLC
from src.signals.confidence import apply_htf_alignment, apply_intrabar_decay
from src.signals.evaluation.regime import MarketRegimeDetector, RegimeType
from src.signals.models import SignalDecision
from src.signals.service import SignalModule
from src.trading.pending_entry import PendingEntryConfig, compute_entry_zone
from src.trading.sizing import RegimeSizing, compute_trade_params

from .data_loader import HistoricalDataLoader
from .filters import BacktestFilterConfig, BacktestFilterSimulator
from .metrics import compute_metrics, compute_metrics_grouped
from .models import (
    BacktestConfig,
    BacktestResult,
    SignalEvaluation,
    generate_run_id,
)
from .portfolio import PortfolioTracker

logger = logging.getLogger(__name__)


class _CircuitBreaker:
    """回测用连败熔断器（基于 bar 计数，非墙钟时间）。"""

    def __init__(self, max_consecutive_losses: int = 5, cooldown_bars: int = 20) -> None:
        self._max_losses = max_consecutive_losses
        self._cooldown_bars = cooldown_bars
        self._consecutive_losses: int = 0
        self._paused: bool = False
        self._paused_at_bar: int = 0
        self.total_pauses: int = 0

    def record_outcome(self, won: bool, bar_index: int) -> None:
        if won:
            self._consecutive_losses = 0
            return
        self._consecutive_losses += 1
        if not self._paused and self._consecutive_losses >= self._max_losses:
            self._paused = True
            self._paused_at_bar = bar_index
            self.total_pauses += 1

    def is_paused(self, bar_index: int) -> bool:
        if not self._paused:
            return False
        if self._cooldown_bars > 0 and (bar_index - self._paused_at_bar) >= self._cooldown_bars:
            self._paused = False
            self._consecutive_losses = 0
            return False
        return True


@dataclass
class _BacktestSignalState:
    """回测信号状态机状态（模拟实盘 preview→armed→confirmed 转换）。"""

    current_action: str = "hold"  # hold / buy / sell
    stable_bars: int = 0
    armed: bool = False


class BacktestEngine:
    """回测引擎：逐 bar 回放历史数据，复用生产指标和策略组件。

    核心设计：
    - 直接复用 OptimizedPipeline.compute() 计算指标
    - 直接复用 SignalModule.evaluate() 评估策略（含完整置信度管线）
    - persist=False 避免写入生产 DB
    - 滚动窗口传入 bar 数据（窗口大小 = warmup_bars）
    - 可选启用过滤器模拟（复现实盘 SignalFilterChain 行为）
    - 信号评估记录（对应实盘 signal_outcomes，回测用 N bars 回填）
    """

    def __init__(
        self,
        config: BacktestConfig,
        data_loader: HistoricalDataLoader,
        signal_module: SignalModule,
        indicator_pipeline: Any,  # OptimizedPipeline
        regime_detector: Optional[MarketRegimeDetector] = None,
        voting_engine: Optional[Any] = None,  # StrategyVotingEngine（单 consensus）
        voting_group_engines: Optional[List[Any]] = None,  # [(VotingGroupConfig, Engine)]
        performance_tracker: Optional[Any] = None,  # StrategyPerformanceTracker
        # 置信度后处理（复用实盘 SignalRuntime 管线）
        intrabar_confidence_factor: float = 0.85,
        htf_direction_fn: Optional[Callable[[str, str], Optional[str]]] = None,
        htf_alignment_boost: float = 1.10,
        htf_conflict_penalty: float = 0.70,
        # HTF 指标预计算数据（从更高时间框架加载）
        htf_indicator_data: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
        # 性能优化：预计算指标快照（优化器复用时避免重复计算）
        precomputed_indicators: Optional[List[Dict[str, Dict[str, Any]]]] = None,
    ) -> None:
        self._config = config
        self._data_loader = data_loader
        self._signal_module = signal_module
        self._pipeline = indicator_pipeline
        self._regime_detector = regime_detector or MarketRegimeDetector()
        self._voting_engine = voting_engine
        self._voting_group_engines: List[Any] = voting_group_engines or []
        self._performance_tracker = performance_tracker
        # 属于 voting group 的策略（不单独 process_decision，只贡献投票）
        self._voting_group_members: frozenset = frozenset(
            name
            for group_cfg, _eng in self._voting_group_engines
            for name in group_cfg.strategies
        )

        # 置信度后处理参数（与实盘 SignalRuntime 相同）
        self._intrabar_confidence_factor = intrabar_confidence_factor
        self._htf_direction_fn = htf_direction_fn
        self._htf_alignment_boost = htf_alignment_boost
        self._htf_conflict_penalty = htf_conflict_penalty
        # HTF 指标：{timeframe: {indicator_name: {field: value}}}（静态快照，向后兼容）
        self._htf_indicator_data = htf_indicator_data or {}
        # HTF 时序数据：{timeframe: [(bar_time, indicators), ...]}，按 bar 时间查找
        self._htf_timeseries: Dict[str, List[tuple[datetime, Dict[str, Dict[str, Any]]]]] = {}
        # 预计算的时间索引（避免每次 _lookup_htf_at_time 都生成临时列表）
        self._htf_time_indexes: Dict[str, List[datetime]] = {}
        # 预计算指标快照（按 all_bars 索引对齐，优化器复用场景下跳过 pipeline）
        self._precomputed_indicators = precomputed_indicators

        # 指标驱动出场配置
        from src.trading.position_rules import IndicatorExitConfig
        indicator_exit_cfg = IndicatorExitConfig(
            enabled=config.indicator_exit_enabled,
            supertrend_enabled=config.indicator_exit_supertrend_enabled,
            supertrend_tighten_atr=config.indicator_exit_supertrend_tighten_atr,
            rsi_enabled=config.indicator_exit_rsi_enabled,
            rsi_overbought=config.indicator_exit_rsi_overbought,
            rsi_oversold=config.indicator_exit_rsi_oversold,
            rsi_delta_threshold=config.indicator_exit_rsi_delta_threshold,
            rsi_tighten_atr=config.indicator_exit_rsi_tighten_atr,
            macd_enabled=config.indicator_exit_macd_enabled,
            macd_tighten_atr=config.indicator_exit_macd_tighten_atr,
            adx_enabled=config.indicator_exit_adx_enabled,
            adx_entry_min=config.indicator_exit_adx_entry_min,
            adx_collapse_threshold=config.indicator_exit_adx_collapse_threshold,
            adx_tighten_atr=config.indicator_exit_adx_tighten_atr,
        )

        # 连败熔断器
        self._circuit_breaker: _CircuitBreaker | None = None
        if config.circuit_breaker_enabled:
            self._circuit_breaker = _CircuitBreaker(
                max_consecutive_losses=config.circuit_breaker_max_consecutive_losses,
                cooldown_bars=config.circuit_breaker_cooldown_bars,
            )

        self._portfolio = PortfolioTracker(
            initial_balance=config.initial_balance,
            max_positions=config.max_positions,
            trailing_tp_enabled=config.trailing_tp_enabled,
            trailing_tp_activation_atr=config.trailing_tp_activation_atr,
            trailing_tp_trail_atr=config.trailing_tp_trail_atr,
            commission_per_lot=config.commission_per_lot,
            slippage_points=config.slippage_points,
            contract_size=config.contract_size,
            min_volume=config.min_volume,
            max_volume=config.max_volume,
            max_volume_per_order=config.max_volume_per_order,
            max_volume_per_symbol=config.max_volume_per_symbol,
            max_volume_per_day=config.max_volume_per_day,
            daily_loss_limit_pct=config.daily_loss_limit_pct,
            max_trades_per_day=config.max_trades_per_day,
            max_trades_per_hour=config.max_trades_per_hour,
            trailing_atr_multiplier=config.trailing_atr_multiplier,
            breakeven_atr_threshold=config.breakeven_atr_threshold,
            end_of_day_close_enabled=config.end_of_day_close_enabled,
            end_of_day_close_hour_utc=config.end_of_day_close_hour_utc,
            end_of_day_close_minute_utc=config.end_of_day_close_minute_utc,
            indicator_exit_config=indicator_exit_cfg,
        )

        # Pending Entry 配置（复用实盘 compute_entry_zone 纯函数）
        self._pending_entry_enabled = config.pending_entry_enabled
        self._pending_entry_config = PendingEntryConfig(
            pullback_atr_factor=config.pending_entry_pullback_atr_factor,
            chase_atr_factor=config.pending_entry_chase_atr_factor,
            momentum_atr_factor=config.pending_entry_momentum_atr_factor,
            symmetric_atr_factor=config.pending_entry_symmetric_atr_factor,
        )
        # 挂起的入场意图：{signal_key: (decision, entry_low, entry_high, expiry_bar)}
        self._pending_entries: Dict[str, Tuple[SignalDecision, float, float, int]] = {}

        # 确定目标策略列表，并过滤掉回测 SignalModule 不支持的名称。
        available_strategies = set(self._signal_module.list_strategies())
        if config.strategies:
            requested_strategies = []
            seen_requested: Set[str] = set()
            for strategy in config.strategies:
                name = str(strategy).strip()
                if not name or name in seen_requested:
                    continue
                requested_strategies.append(name)
                seen_requested.add(name)

            unsupported = [
                strategy for strategy in requested_strategies
                if strategy not in available_strategies
            ]
            if unsupported:
                logger.warning(
                    "BacktestEngine: ignoring unsupported strategies: %s",
                    ", ".join(sorted(unsupported)),
                )
            self._target_strategies = [
                strategy for strategy in requested_strategies
                if strategy in available_strategies
            ]
            if not self._target_strategies:
                raise ValueError(
                    "No supported strategies remain after filtering requested strategies: "
                    f"{requested_strategies}"
                )
        else:
            self._target_strategies = sorted(available_strategies)

        # 按 strategy_timeframes 白名单过滤（与实盘 SignalRuntime 行为一致）
        tf_whitelist = config.strategy_timeframes
        if tf_whitelist:
            tf_upper = config.timeframe.upper()
            before_count = len(self._target_strategies)
            self._target_strategies = [
                s for s in self._target_strategies
                if s not in tf_whitelist
                or tf_upper in [t.upper() for t in tf_whitelist[s]]
            ]
            filtered = before_count - len(self._target_strategies)
            if filtered > 0:
                logger.info(
                    "Backtest: filtered %d strategies not allowed on %s by strategy_timeframes",
                    filtered, config.timeframe,
                )

        # per-strategy session 限制（与实盘 SignalRuntime.strategy_sessions 行为一致）
        from src.signals.contracts import normalize_session_name
        self._strategy_sessions: Dict[str, tuple[str, ...]] = {
            name: tuple(normalize_session_name(s) for s in sessions)
            for name, sessions in config.strategy_sessions.items()
            if sessions
        }

        # 用于在 run() 和 _evaluate_strategies() 中查询 bar 的 current_sessions
        self._session_filter: Optional[Any] = None
        if self._strategy_sessions:
            from src.signals.execution.filters import SessionFilter
            self._session_filter = SessionFilter(allowed_sessions=("london", "newyork", "asia"))

        # 收集所有目标策略需要的指标名 + regime 检测需要的指标
        self._required_indicators: List[str] = []
        seen: Set[str] = set()
        for s in self._target_strategies:
            for ind in self._signal_module.strategy_requirements(s):
                if ind not in seen:
                    seen.add(ind)
                    self._required_indicators.append(ind)
        # regime 检测依赖 adx14 + boll20 + keltner20，缺失时全部降级为 uncertain
        for regime_ind in ("adx14", "boll20", "keltner20"):
            if regime_ind not in seen:
                seen.add(regime_ind)
                self._required_indicators.append(regime_ind)

        # 构建过滤器模拟器
        self._filter_simulator = self._build_filter_simulator()

        # 信号状态机（模拟实盘 preview→armed→confirmed 状态转换）
        self._signal_states: Dict[str, _BacktestSignalState] = {}
        if config.enable_state_machine:
            for s in self._target_strategies:
                self._signal_states[s] = _BacktestSignalState()

        # 信号评估记录（用于回测质量分析 + 数据落表）
        self._signal_evaluations: List[SignalEvaluation] = []
        # 待回填的 pending 评估：{bar_index: [SignalEvaluation]}
        self._pending_evaluations: Dict[int, List[SignalEvaluation]] = {}
        self._bars_to_evaluate = 5  # N bars 后回填
        # 去重：已记录的 (bar_index, strategy) 组合
        self._recorded_evals: Set[Tuple[int, str]] = set()

    def _build_filter_simulator(self) -> BacktestFilterSimulator:
        """从 BacktestConfig 构建过滤器模拟器。"""
        # 经济事件过滤：从 DB 预加载历史事件
        economic_provider = None
        if self._config.filter_economic_enabled:
            economic_provider = self._load_economic_provider()

        filter_config = BacktestFilterConfig(
            enabled=self._config.filters_enabled,
            session_filter_enabled=self._config.filter_session_enabled,
            allowed_sessions=self._config.filter_allowed_sessions,
            session_transition_enabled=self._config.filter_session_transition_enabled,
            session_transition_cooldown_minutes=self._config.filter_session_transition_cooldown,
            volatility_filter_enabled=self._config.filter_volatility_enabled,
            volatility_spike_multiplier=self._config.filter_volatility_spike_multiplier,
            spread_filter_enabled=self._config.filter_spread_enabled,
            max_spread_points=self._config.filter_max_spread_points,
            economic_filter_enabled=self._config.filter_economic_enabled and economic_provider is not None,
            economic_provider=economic_provider,
            economic_lookahead_minutes=self._config.filter_economic_lookahead_minutes,
            economic_lookback_minutes=self._config.filter_economic_lookback_minutes,
            economic_importance_min=self._config.filter_economic_importance_min,
        )
        return BacktestFilterSimulator(filter_config)

    def _load_economic_provider(self) -> Any:
        """从 DB 加载回测期间的经济事件，构建 BacktestTradeGuardProvider。"""
        try:
            from .economic_provider import (
                BacktestTradeGuardProvider,
                _SimpleSettings,
                load_backtest_economic_events,
            )
            from src.persistence.db import TimescaleWriter
            from src.config.database import load_db_settings
            from src.persistence.repositories.economic_repo import EconomicCalendarRepository
            from src.calendar.economic_calendar.trade_guard import infer_symbol_context

            settings = load_db_settings()
            writer = TimescaleWriter(settings, min_conn=1, max_conn=2)
            repo = EconomicCalendarRepository(writer)

            context = infer_symbol_context(self._config.symbol)
            events = load_backtest_economic_events(
                economic_repo=repo,
                start_time=self._config.start_time,
                end_time=self._config.end_time,
                currencies=context["currencies"] or None,
                importance_min=self._config.filter_economic_importance_min,
            )
            writer.close()

            if not events:
                logger.info("No economic events found for backtest period, economic filter disabled")
                return None

            # 从 economic.ini 加载 relevance 配置
            try:
                from src.config.runtime import load_runtime_config
                rt_cfg = load_runtime_config()
                eco_settings = _SimpleSettings(
                    trade_guard_relevance_filter_enabled=getattr(
                        rt_cfg, "trade_guard_relevance_filter_enabled", False
                    ),
                    gold_impact_keywords=getattr(rt_cfg, "gold_impact_keywords", ""),
                )
            except Exception:
                eco_settings = _SimpleSettings()

            return BacktestTradeGuardProvider(events, eco_settings)
        except Exception:
            logger.warning("Failed to load economic calendar for backtest", exc_info=True)
            return None

    def run(self) -> BacktestResult:
        """执行回测主循环。"""
        run_id = generate_run_id()
        started_at = datetime.now(timezone.utc)
        t0 = time.monotonic()

        symbol = self._config.symbol
        timeframe = self._config.timeframe
        warmup_count = self._config.warmup_bars

        # 1. 加载数据
        logger.info(
            "Backtest %s: loading data for %s/%s [%s ~ %s]",
            run_id,
            symbol,
            timeframe,
            self._config.start_time.isoformat(),
            self._config.end_time.isoformat(),
        )
        warmup_bars = self._data_loader.preload_warmup_bars(
            symbol, timeframe, self._config.start_time, warmup_count
        )
        test_bars = self._data_loader.load_all_bars(
            symbol, timeframe, self._config.start_time, self._config.end_time
        )

        # warmup 数据不足时，从 test_bars 前部借用
        if len(warmup_bars) < warmup_count and len(test_bars) > warmup_count:
            borrow = warmup_count - len(warmup_bars)
            warmup_bars = warmup_bars + test_bars[:borrow]
            test_bars = test_bars[borrow:]
            logger.info(
                "Backtest: borrowed %d bars from test data for warmup "
                "(total warmup=%d, remaining test=%d)",
                borrow, len(warmup_bars), len(test_bars),
            )

        if not test_bars:
            logger.warning("Backtest %s: no test data found", run_id)
            completed_at = datetime.now(timezone.utc)
            from .metrics import _empty_metrics

            return BacktestResult(
                config=self._config,
                run_id=run_id,
                started_at=started_at,
                completed_at=completed_at,
                trades=[],
                equity_curve=[],
                metrics=_empty_metrics(),
                metrics_by_regime={},
                metrics_by_strategy={},
                metrics_by_confidence={},
                param_set=self._config.strategy_params,
                filter_stats=None,
                signal_evaluations=[],
            )

        all_bars = warmup_bars + test_bars
        warmup_end = len(warmup_bars)

        logger.info(
            "Backtest %s: %d warmup bars + %d test bars = %d total",
            run_id,
            len(warmup_bars),
            len(test_bars),
            len(all_bars),
        )

        # 1.5 自动预加载 HTF 指标（如果启用了 HTF 对齐但未手动传入 htf_indicator_data）
        if (
            self._config.enable_htf_alignment
            and not self._htf_indicator_data
            and self._htf_direction_fn is None
        ):
            # 自动推断 HTF 时间框架：比当前 TF 更高的已配置 TF
            _TF_RANK = {"M1": 1, "M5": 2, "M15": 3, "M30": 4, "H1": 5, "H4": 6, "D1": 7}
            current_rank = _TF_RANK.get(timeframe, 0)
            htf_list = [
                tf for tf, rank in _TF_RANK.items()
                if rank > current_rank and rank <= current_rank + 3
            ]
            if htf_list:
                self._htf_indicator_data = self.preload_htf_indicators(
                    symbol, htf_list,
                    self._config.start_time, self._config.end_time,
                )

        # 2. 如果没有预计算指标，一次性预计算全部（避免主循环内重复 pipeline 调用）
        if self._precomputed_indicators is not None:
            all_indicator_snapshots = self._precomputed_indicators
        else:
            all_indicator_snapshots = self._precompute_all_indicators(
                symbol, timeframe, all_bars, warmup_count
            )

        # 3. 逐 bar 回放
        equity_sample_interval = max(1, len(test_bars) // 500)  # 最多 500 个采样点

        for i in range(warmup_end, len(all_bars)):
            bar = all_bars[i]
            bar_index = i - warmup_end
            self._portfolio.observe_bar(bar)

            # 直接取预计算的指标快照
            indicators = all_indicator_snapshots[i] if i < len(all_indicator_snapshots) else {}

            # 4. Regime 检测
            regime: Optional[RegimeType] = None
            soft_regime_dict: Optional[Dict[str, Any]] = None
            if indicators:
                regime, soft_regime_dict = self._detect_regime(indicators)

            # 5. 检查持仓 SL/TP + 指标驱动出场
            closed_trades = self._portfolio.check_exits(bar, bar_index, indicators)
            # 连败熔断器 + PerformanceTracker：记录交易结果
            if closed_trades:
                for trade in closed_trades:
                    if self._circuit_breaker is not None:
                        self._circuit_breaker.record_outcome(trade.pnl > 0, bar_index)
                    if self._performance_tracker is not None:
                        try:
                            self._performance_tracker.record_outcome(
                                strategy=trade.strategy,
                                won=trade.pnl > 0,
                                pnl=trade.pnl,
                                regime=trade.regime,
                                source="trade",
                            )
                        except Exception:
                            pass

            # 5.1 检查 Pending Entry 是否可填单（每根 bar 都需要检查）
            if self._pending_entry_enabled and indicators and regime is not None:
                self._check_pending_entries(bar, bar_index, indicators, regime)

            # 5.5 回填待评估信号（N bars 后用当前价格回填结果）
            self._backfill_evaluations(bar_index, bar.close)

            if not indicators or regime is None:
                continue

            # 6. 过滤器检查（模拟实盘 SignalFilterChain，需在 indicators 校验之后）
            filter_allowed, filter_reason = self._filter_simulator.should_evaluate(
                symbol=symbol,
                bar_time=bar.time,
                indicators=indicators,
            )

            if not filter_allowed:
                # 记录被过滤的信号评估（所有策略标记为 filtered）
                for strategy_name in self._target_strategies:
                    self._record_evaluation(
                        bar=bar,
                        bar_index=bar_index,
                        strategy=strategy_name,
                        action="hold",
                        confidence=0.0,
                        regime=regime.value,
                        filtered=True,
                        filter_reason=filter_reason,
                    )
                continue

            # 6.5. 按当前 bar 时间查找对应 HTF 指标（动态时序查找）
            if self._htf_timeseries:
                self._htf_indicator_data = self._lookup_htf_at_time(bar.time)

            # 7. 策略评估
            decisions = self._evaluate_strategies(
                symbol, timeframe, indicators, regime, soft_regime_dict,
                bar_time=bar.time,
            )

            # 8. 记录信号评估 + 处理独立策略信号
            #    属于 voting group 的策略不单独 process_decision（与实盘一致）
            for decision in decisions:
                self._record_evaluation(
                    bar=bar,
                    bar_index=bar_index,
                    strategy=decision.strategy,
                    action=decision.direction,
                    confidence=decision.confidence,
                    regime=regime.value,
                )
                if decision.strategy not in self._voting_group_members:
                    self._process_decision(decision, bar, bar_index, indicators, regime)

            # 9. 投票引擎（多组模式 + 单 consensus 模式）
            actionable = [d for d in decisions if d.direction in ("buy", "sell")]
            if actionable:
                try:
                    # 多组模式：每组独立投票，产生独立信号
                    if self._voting_group_engines:
                        for group_cfg, group_engine in self._voting_group_engines:
                            group_decisions = [
                                d for d in actionable
                                if d.strategy in group_cfg.strategies
                            ]
                            if group_decisions:
                                vote = group_engine.vote(
                                    group_decisions,
                                    regime=regime,
                                    scope="confirmed",
                                    exclude_composite=False,
                                )
                                if vote is not None:
                                    self._record_evaluation(
                                        bar=bar, bar_index=bar_index,
                                        strategy=vote.strategy,
                                        action=vote.direction,
                                        confidence=vote.confidence,
                                        regime=regime.value,
                                    )
                                    self._process_decision(
                                        vote, bar, bar_index, indicators, regime
                                    )
                    # 单 consensus 模式
                    elif self._voting_engine is not None:
                        consensus = self._voting_engine.vote(
                            actionable, regime=regime, scope="confirmed"
                        )
                        if consensus is not None:
                            self._record_evaluation(
                                bar=bar, bar_index=bar_index,
                                strategy=consensus.strategy,
                                action=consensus.direction,
                                confidence=consensus.confidence,
                                regime=regime.value,
                            )
                            self._process_decision(
                                consensus, bar, bar_index, indicators, regime
                            )
                except Exception:
                    logger.warning(
                        "VotingEngine failed at bar %d", bar_index, exc_info=True
                    )

            # 10. 记录资金曲线（采样）
            if bar_index % equity_sample_interval == 0:
                self._portfolio.record_equity(bar)

        # 11. 最终资金快照
        if test_bars:
            self._portfolio.record_equity(test_bars[-1])

        # 12. 强制平仓剩余持仓
        last_bar = all_bars[-1]
        last_index = len(all_bars) - 1 - warmup_end
        self._portfolio.close_all(last_bar, last_index)

        # 13. 回填所有剩余的 pending 评估（用最后价格）
        self._flush_pending_evaluations(last_bar.close)

        # 14. 汇总结果
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        completed_at = datetime.now(timezone.utc)

        trades = self._portfolio.closed_trades
        equity_curve = self._portfolio.equity_curve
        equity_values = self._portfolio.equity_values

        metrics = compute_metrics(trades, self._config.initial_balance, equity_values)
        metrics_by_regime = compute_metrics_grouped(
            trades, self._config.initial_balance, equity_values, "regime"
        )
        metrics_by_strategy = compute_metrics_grouped(
            trades, self._config.initial_balance, equity_values, "strategy"
        )
        metrics_by_confidence = compute_metrics_grouped(
            trades, self._config.initial_balance, equity_values, "confidence_level"
        )

        # 过滤器统计
        filter_stats = self._filter_simulator.stats.to_dict()

        logger.info(
            "Backtest %s completed: %d trades, win_rate=%.2f%%, PnL=%.2f, "
            "filter_pass_rate=%.1f%%, elapsed=%dms",
            run_id,
            metrics.total_trades,
            metrics.win_rate * 100,
            metrics.total_pnl,
            self._filter_simulator.stats.pass_rate * 100,
            elapsed_ms,
        )

        return BacktestResult(
            config=self._config,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            trades=trades,
            equity_curve=equity_curve,
            metrics=metrics,
            metrics_by_regime=metrics_by_regime,
            metrics_by_strategy=metrics_by_strategy,
            metrics_by_confidence=metrics_by_confidence,
            param_set=self._config.strategy_params,
            filter_stats=filter_stats,
            signal_evaluations=self._signal_evaluations,
        )

    def _precompute_all_indicators(
        self,
        symbol: str,
        timeframe: str,
        all_bars: List[OHLC],
        warmup_count: int,
    ) -> List[Dict[str, Dict[str, Any]]]:
        """一次性预计算所有 bar 位置的指标快照。

        对每个 bar 位置构建滑动窗口并计算指标，结果列表按 all_bars 索引对齐。
        优化器可将此结果缓存并在多次迭代中复用（指标值不随策略参数变化）。
        """
        t0 = time.monotonic()
        snapshots: List[Dict[str, Dict[str, Any]]] = []
        for i in range(len(all_bars)):
            window_start = max(0, i - warmup_count)
            window = all_bars[window_start : i + 1]
            indicators = self._compute_indicators(symbol, timeframe, window)
            snapshots.append(indicators)
        elapsed = int((time.monotonic() - t0) * 1000)
        logger.info(
            "Pre-computed indicators for %d bars in %dms", len(all_bars), elapsed
        )
        return snapshots

    _SENTINEL = object()

    def _compute_indicators(
        self,
        symbol: str,
        timeframe: str,
        bars: List[OHLC],
        indicator_names: Any = _SENTINEL,
    ) -> Dict[str, Dict[str, Any]]:
        """使用生产 Pipeline 计算指标。

        Args:
            indicator_names: 指定计算哪些指标。
                不传（sentinel）= 使用 _required_indicators；
                传 None = 计算全部已注册指标。
        """
        if len(bars) < 2:
            return {}
        names = self._required_indicators if indicator_names is self._SENTINEL else indicator_names
        try:
            results = self._pipeline.compute(
                symbol, timeframe, bars, names
            )
            return results
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("Indicator computation failed: %s", e)
            return {}
        except Exception:
            logger.warning("Unexpected indicator computation error", exc_info=True)
            return {}

    def _detect_regime(
        self, indicators: Dict[str, Dict[str, Any]]
    ) -> Tuple[Optional[RegimeType], Optional[Dict[str, Any]]]:
        """检测市场 Regime。异常时返回 (None, None) 跳过该 bar。"""
        try:
            regime = self._regime_detector.detect(indicators)
        except Exception:
            logger.debug("Regime detection failed", exc_info=True)
            return None, None
        soft_regime_dict: Optional[Dict[str, Any]] = None
        try:
            soft_result = self._regime_detector.detect_soft(indicators)
            if soft_result is not None:
                soft_regime_dict = soft_result.to_dict()
        except Exception:
            pass
        return regime, soft_regime_dict

    def preload_htf_indicators(
        self,
        symbol: str,
        htf_timeframes: List[str],
        start_time: datetime,
        end_time: datetime,
        warmup_bars: int = 200,
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """预加载高时间框架指标的时序数据，供逐 bar 查找。

        返回格式与旧版兼容（最终快照），但同时填充 ``_htf_timeseries``
        以支持按 bar 时间查找对应时点的 HTF 指标值。

        Returns:
            {timeframe: {indicator_name: {field: value}}} 最新快照（向后兼容）
        """
        htf_data: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for tf in htf_timeframes:
            try:
                warmup = self._data_loader.preload_warmup_bars(
                    symbol, tf, start_time, warmup_bars
                )
                htf_bars = self._data_loader.load_all_bars(
                    symbol, tf, start_time, end_time
                )
                all_htf = warmup + htf_bars
                if len(all_htf) < 2:
                    logger.warning(
                        "Backtest HTF: insufficient bars for %s/%s (%d)",
                        symbol, tf, len(all_htf),
                    )
                    continue
                # 逐 bar 计算 HTF 指标（滚动窗口），建立时序索引
                ts_list: List[tuple[datetime, Dict[str, Dict[str, Any]]]] = []
                window_size = min(warmup_bars, len(all_htf))
                for end_idx in range(window_size, len(all_htf) + 1):
                    window = all_htf[max(0, end_idx - window_size):end_idx]
                    # HTF 计算全量指标（不限于 M5 策略需要的子集）
                    snap = self._compute_indicators(symbol, tf, window, indicator_names=None)
                    if snap:
                        bar_time = window[-1].time
                        ts_list.append((bar_time, snap))
                if ts_list:
                    self._htf_timeseries[tf] = ts_list
                    # 预计算时间索引（避免 _lookup_htf_at_time 每次生成临时列表）
                    self._htf_time_indexes[tf] = [t[0] for t in ts_list]
                    # 向后兼容：最终快照
                    htf_data[tf] = ts_list[-1][1]
                    logger.info(
                        "Backtest HTF: loaded %d bars for %s/%s, %d indicators",
                        len(all_htf), symbol, tf, len(ts_list[-1][1]),
                    )
            except Exception:
                logger.warning(
                    "Backtest HTF: failed to load %s/%s", symbol, tf, exc_info=True
                )
        return htf_data

    def _lookup_htf_at_time(
        self, bar_time: datetime,
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """按 bar 时间查找对应时点的 HTF 指标（二分查找最近的已收盘 HTF bar）。"""
        from bisect import bisect_right
        result: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for tf, ts_list in self._htf_timeseries.items():
            if not ts_list:
                continue
            times = self._htf_time_indexes.get(tf)
            if times is None:
                continue
            idx = bisect_right(times, bar_time) - 1
            if idx >= 0:
                result[tf] = ts_list[idx][1]
        return result

    def _evaluate_strategies(
        self,
        symbol: str,
        timeframe: str,
        indicators: Dict[str, Dict[str, Any]],
        regime: RegimeType,
        soft_regime_dict: Optional[Dict[str, Any]],
        scope: str = "confirmed",
        bar_time: Optional[datetime] = None,
    ) -> List[SignalDecision]:
        """评估所有目标策略。

        复用实盘置信度管线：
        1. SignalModule.evaluate() — affinity / performance / calibrator / floor
        2. apply_intrabar_decay() — intrabar scope 衰减（复用 src/signals/confidence）
        3. apply_htf_alignment() — HTF 方向对齐修正（复用 src/signals/confidence）
        """
        metadata: Dict[str, Any] = {"_regime": regime.value}
        if soft_regime_dict is not None:
            metadata["_soft_regime"] = soft_regime_dict
        # 置信度管线配置开关（通过 metadata 标记传递给 SignalModule.evaluate）
        if not self._config.enable_regime_affinity:
            metadata["_pre_computed_affinity"] = 1.0
        if not self._config.enable_performance_tracker:
            metadata["_skip_performance_tracker"] = True
        if not self._config.enable_calibrator:
            metadata["_skip_calibrator"] = True

        # per-strategy session 过滤：计算当前 bar 所属 session
        current_sessions: List[str] = []
        if self._strategy_sessions and self._session_filter and bar_time is not None:
            current_sessions = self._session_filter.current_sessions(bar_time)

        decisions: List[SignalDecision] = []
        for strategy_name in self._target_strategies:
            try:
                # per-strategy session 过滤（与实盘 SignalRuntime 行为一致）
                allowed_sessions = self._strategy_sessions.get(strategy_name, ())
                if allowed_sessions and current_sessions:
                    if not any(s in allowed_sessions for s in current_sessions):
                        continue

                # 检查策略所需指标是否都已计算
                required = self._signal_module.strategy_requirements(strategy_name)
                missing = [ind for ind in required if ind not in indicators]
                if missing:
                    continue

                # 仅传入该策略需要的指标
                scoped_indicators = {
                    ind: indicators[ind] for ind in required if ind in indicators
                }

                decision = self._signal_module.evaluate(
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy=strategy_name,
                    indicators=scoped_indicators,
                    metadata=metadata,
                    persist=False,
                    htf_indicators=self._htf_indicator_data,
                )

                # ── 置信度后处理（与实盘 SignalRuntime 顺序一致）──
                # 1. HTF 方向对齐修正（先作用于原始置信度）
                if (
                    self._config.enable_htf_alignment
                    and self._htf_direction_fn is not None
                ):
                    htf_dir = self._htf_direction_fn(symbol, timeframe)
                    decision = apply_htf_alignment(
                        decision,
                        htf_direction=htf_dir,
                        alignment_boost=self._htf_alignment_boost,
                        conflict_penalty=self._htf_conflict_penalty,
                    )
                # 2. Intrabar 衰减（最后降权，与实盘顺序一致）
                decision = apply_intrabar_decay(
                    decision, scope, self._intrabar_confidence_factor
                )

                decisions.append(decision)
            except (KeyError, TypeError, ValueError) as e:
                logger.warning(
                    "Strategy %s evaluation failed: %s", strategy_name, e
                )
            except Exception:
                logger.warning(
                    "Strategy %s unexpected error", strategy_name, exc_info=True
                )
        return decisions

    def _update_state_machine(
        self,
        strategy: str,
        action: str,
        confidence: float,
    ) -> bool:
        """更新信号状态机，返回是否允许执行交易。

        模拟实盘 preview→armed→confirmed 状态转换：
        - 方向与上一 bar 相同：stable_bars 递增
        - 方向改变：重置状态，stable_bars = 1
        - stable_bars >= min_preview_stable_bars：标记为 armed
        - 仅 armed 且方向为 buy/sell 时返回 True
        """
        state = self._signal_states.get(strategy)
        if state is None:
            # 惰性初始化：动态添加的策略也能参与状态机追踪
            state = _BacktestSignalState()
            self._signal_states[strategy] = state

        if action == state.current_action:
            state.stable_bars += 1
        else:
            state.current_action = action
            state.stable_bars = 1
            state.armed = False

        if state.stable_bars >= self._config.min_preview_stable_bars:
            state.armed = True

        return state.armed and action in ("buy", "sell")

    def _process_decision(
        self,
        decision: SignalDecision,
        bar: OHLC,
        bar_index: int,
        indicators: Dict[str, Dict[str, Any]],
        regime: RegimeType,
    ) -> None:
        """处理单个信号决策：开仓或反向关仓。"""
        if decision.direction not in ("buy", "sell"):
            # 状态机仍需更新 hold 状态（重置方向）
            if self._config.enable_state_machine:
                self._update_state_machine(
                    decision.strategy, decision.direction, decision.confidence
                )
            return
        if decision.confidence < self._config.min_confidence:
            return

        # 连败熔断器检查
        if self._circuit_breaker is not None and self._circuit_breaker.is_paused(bar_index):
            return

        # 信号状态机门控：方向需稳定 N bars 后才允许执行
        if self._config.enable_state_machine:
            armed = self._update_state_machine(
                decision.strategy, decision.direction, decision.confidence
            )
            if not armed:
                logger.debug(
                    "State machine: %s %s not armed yet (stable_bars=%d/%d)",
                    decision.strategy,
                    decision.direction,
                    self._signal_states[decision.strategy].stable_bars,
                    self._config.min_preview_stable_bars,
                )
                return

        # 不做 signal_exit 反手平仓 — 与实盘 TradeExecutor 行为一致：
        # 实盘中反向信号直接开新仓（如果持仓数未满），旧仓由 SL/TP/Trailing 自然出场。
        # 旧逻辑会以 bar.close 平仓旧仓（signal_exit），放弃 trailing SL 已锁定的利润。

        # 检查同策略同方向是否已有持仓（避免重复开仓）
        for pos in self._portfolio._open_positions:
            if pos.strategy == decision.strategy and pos.direction == decision.direction:
                return  # 已有同方向持仓，跳过

        # 获取 ATR 值用于 sizing
        atr_data = indicators.get("atr14", {})
        atr_value = atr_data.get("atr", 0.0)
        if atr_value <= 0:
            return

        # Pending Entry 模拟：不立即开仓，等后续 bar 价格落入区间
        if self._pending_entry_enabled:
            category = decision.metadata.get("category", "trend")
            from src.trading.pending_entry import _CATEGORY_ZONE_MODE

            zone_mode = _CATEGORY_ZONE_MODE.get(category, "symmetric")
            entry_low, entry_high = compute_entry_zone(
                action=decision.direction,
                close_price=bar.close,
                atr=atr_value,
                zone_mode=zone_mode,
                config=self._pending_entry_config,
                strategy_name=decision.strategy,
                category=category,
                indicators=indicators,
                timeframe=self._config.timeframe,
            )
            expiry_bar = bar_index + self._config.pending_entry_expiry_bars
            key = f"{decision.strategy}_{decision.direction}"
            self._pending_entries[key] = (decision, entry_low, entry_high, expiry_bar)
            return

        self._execute_entry(decision, bar, bar_index, atr_value, regime, indicators)

    def _check_pending_entries(
        self,
        bar: OHLC,
        bar_index: int,
        indicators: Dict[str, Dict[str, Any]],
        regime: RegimeType,
    ) -> None:
        """检查挂起的入场意图，价格落入区间则执行开仓。"""
        if not self._pending_entries:
            return

        filled_keys: List[str] = []
        for key, (decision, entry_low, entry_high, expiry_bar) in self._pending_entries.items():
            # 检查超时
            if bar_index > expiry_bar:
                logger.debug(
                    "Pending entry expired: %s %s at bar %d (expiry=%d)",
                    decision.strategy, decision.direction, bar_index, expiry_bar,
                )
                filled_keys.append(key)
                continue

            # 检查价格是否落入区间（用 bar 的 high/low 模拟盘中价格）
            price_in_zone = bar.low <= entry_high and bar.high >= entry_low
            if price_in_zone:
                # 确定入场价格：取区间中点与 bar 范围的交集
                fill_price = max(entry_low, min(bar.open, entry_high))
                atr_data = indicators.get("atr14", {})
                atr_value = atr_data.get("atr", 0.0)
                if atr_value > 0:
                    self._execute_entry(
                        decision, bar, bar_index, atr_value, regime, indicators
                    )
                filled_keys.append(key)

        for key in filled_keys:
            self._pending_entries.pop(key, None)

    def _execute_entry(
        self,
        decision: SignalDecision,
        bar: OHLC,
        bar_index: int,
        atr_value: float,
        regime: RegimeType,
        indicators: Dict[str, Dict[str, Any]] = {},  # noqa: B006
    ) -> None:
        """执行实际开仓。"""
        try:
            trade_params = compute_trade_params(
                action=decision.direction,
                current_price=bar.close,
                atr_value=atr_value,
                account_balance=self._portfolio.current_balance,
                timeframe=self._config.timeframe,
                risk_percent=self._config.risk_percent,
                min_volume=self._config.min_volume,
                max_volume=self._config.max_volume,
                contract_size=self._config.contract_size,
                regime=regime.value,
                regime_sizing=RegimeSizing(
                    tp_trending=self._config.regime_tp_trending,
                    tp_ranging=self._config.regime_tp_ranging,
                    tp_breakout=self._config.regime_tp_breakout,
                    tp_uncertain=self._config.regime_tp_uncertain,
                    sl_trending=self._config.regime_sl_trending,
                    sl_ranging=self._config.regime_sl_ranging,
                    sl_breakout=self._config.regime_sl_breakout,
                    sl_uncertain=self._config.regime_sl_uncertain,
                ),
            )
        except ValueError as e:
            logger.debug(
                "Trade params computation failed for %s %s: %s",
                decision.strategy, decision.direction, e,
            )
            return

        allowed, reason = self._portfolio.can_open_position(bar, trade_params)
        if not allowed:
            logger.debug(
                "Entry blocked by portfolio risk guard for %s: %s",
                decision.strategy,
                reason,
            )
            return

        self._portfolio.open_position(
            strategy=decision.strategy,
            action=decision.direction,
            bar=bar,
            trade_params=trade_params,
            regime=regime.value,
            confidence=decision.confidence,
            bar_index=bar_index,
            atr_at_entry=atr_value,
            entry_indicators=indicators,
        )

    # ── 信号评估记录与回填 ─────────────────────────────────────────────

    def _record_evaluation(
        self,
        bar: OHLC,
        bar_index: int,
        strategy: str,
        action: str,
        confidence: float,
        regime: str,
        filtered: bool = False,
        filter_reason: str = "",
    ) -> None:
        """记录一次信号评估（对应实盘 SignalQualityTracker）。去重同 bar 同策略。"""
        # 内存保护：超过上限时静默丢弃
        if len(self._signal_evaluations) >= self._config.max_signal_evaluations:
            return
        dedup_key = (bar_index, strategy)
        if dedup_key in self._recorded_evals:
            return
        self._recorded_evals.add(dedup_key)

        eval_record = SignalEvaluation(
            bar_time=bar.time,
            strategy=strategy,
            direction=action,
            confidence=confidence,
            regime=regime,
            price_at_signal=bar.close,
            bars_to_evaluate=self._bars_to_evaluate,
            filtered=filtered,
            filter_reason=filter_reason,
        )

        # 有方向性信号才需要 N bars 后回填
        if action in ("buy", "sell") and not filtered:
            target_index = bar_index + self._bars_to_evaluate
            self._pending_evaluations.setdefault(target_index, []).append(
                eval_record
            )
        else:
            # hold 或被过滤的直接存入
            self._signal_evaluations.append(eval_record)

    def _fill_evaluation(
        self,
        ev: SignalEvaluation,
        exit_price: float,
        incomplete: bool = False,
    ) -> SignalEvaluation:
        """回填单条信号评估的 N bars 后价格和盈亏。

        Args:
            ev: 原始待回填评估
            exit_price: N bars 后的收盘价（或回测结束时的最后价格）
            incomplete: 是否未满 N bars（回测结束时强制回填）
        """
        if ev.direction == "buy":
            pnl_pct = (exit_price - ev.price_at_signal) / ev.price_at_signal * 100
            won = exit_price > ev.price_at_signal
        else:
            pnl_pct = (ev.price_at_signal - exit_price) / ev.price_at_signal * 100
            won = exit_price < ev.price_at_signal

        return SignalEvaluation(
            bar_time=ev.bar_time,
            strategy=ev.strategy,
            direction=ev.direction,
            confidence=ev.confidence,
            regime=ev.regime,
            price_at_signal=ev.price_at_signal,
            price_after_n_bars=exit_price,
            bars_to_evaluate=ev.bars_to_evaluate,
            won=won,
            pnl_pct=round(pnl_pct, 4),
            filtered=ev.filtered,
            filter_reason=ev.filter_reason,
            incomplete=incomplete,
        )

    def _backfill_evaluations(self, current_bar_index: int, current_price: float) -> None:
        """回填已到期的 pending 信号评估（N bars 后用当前价格计算盈亏）。"""
        if current_bar_index not in self._pending_evaluations:
            return

        pending_list = self._pending_evaluations.pop(current_bar_index)
        for ev in pending_list:
            filled = self._fill_evaluation(ev, current_price)
            self._signal_evaluations.append(filled)

    def _flush_pending_evaluations(self, last_price: float) -> None:
        """回测结束时回填所有未到期的 pending 评估（标记为 incomplete）。"""
        for _target_index, pending_list in sorted(self._pending_evaluations.items()):
            for ev in pending_list:
                filled = self._fill_evaluation(ev, last_price, incomplete=True)
                self._signal_evaluations.append(filled)
        self._pending_evaluations.clear()
