"""回测引擎核心：逐 bar 回放历史数据，复用生产指标和策略组件。

设计原则：回测使用实盘方法，不重新实现，避免模拟失真。
- 过滤器：直接复用 SignalFilterChain（via BacktestFilterSimulator）
- 策���评估：直接复用 SignalModule.evaluate()（含完整置信度管线）
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
from src.signals.evaluation.regime import MarketRegimeDetector, RegimeType
from src.signals.models import SignalDecision
from src.signals.service import SignalModule
from src.trading.pending import PendingEntryConfig as TradingPendingEntryConfig

from ..data.loader import HistoricalDataLoader
from ..filtering.builder import build_filter_simulator as _build_filter_simulator_helper
from .indicators import (
    compute_indicators as _compute_indicators_helper,
    detect_regime as _detect_regime_helper,
    lookup_htf_at_time as _lookup_htf_at_time_helper,
    preload_htf_indicators as _preload_htf_indicators_helper,
    precompute_all_indicators as _precompute_all_indicators_helper,
)
from .signals import (
    backfill_evaluations as _backfill_evaluations_helper,
    check_pending_entries as _check_pending_entries_helper,
    evaluate_strategies as _evaluate_strategies_helper,
    flush_pending_evaluations as _flush_pending_evaluations_helper,
    process_decision as _process_decision_helper,
    record_evaluation as _record_evaluation_helper,
)
from ..analysis.metrics import compute_metrics, compute_metrics_grouped
from ..models import (
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


class _BacktestBarService:
    """轻量级适配器：让 MarketStructureAnalyzer 在回测中使用 bar 窗口数据。

    MarketStructureAnalyzer 需要 market_service.get_ohlc_closed(symbol, tf, limit)。
    本类在主循环中通过 update_window() 更新当前 bar 窗口。
    """

    def __init__(self) -> None:
        self._bars: list[Any] = []

    def update_window(self, bars: list[Any]) -> None:
        self._bars = bars

    def get_ohlc_closed(
        self, symbol: str, timeframe: str, limit: int = 200
    ) -> list[Any]:
        return self._bars[-limit:] if len(self._bars) > limit else list(self._bars)


@dataclass
class _BacktestSignalState:
    """回测信号状态机状态（模拟实盘 preview->armed->confirmed 转换）。"""

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

        # 连败熔断器
        cb = config.circuit_breaker
        self._circuit_breaker: _CircuitBreaker | None = None
        if cb.enabled:
            self._circuit_breaker = _CircuitBreaker(
                max_consecutive_losses=cb.max_consecutive_losses,
                cooldown_bars=cb.cooldown_bars,
            )

        # Pending Entry 配置（复用实盘 compute_entry_zone 纯函数）
        pe = config.pending_entry
        self._pending_entry_enabled = pe.enabled
        self._pending_entry_config = TradingPendingEntryConfig(
            pullback_atr_factor=pe.pullback_atr_factor,
            chase_atr_factor=pe.chase_atr_factor,
            momentum_atr_factor=pe.momentum_atr_factor,
            symmetric_atr_factor=pe.symmetric_atr_factor,
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
            self._session_filter = SessionFilter(allowed_sessions=("london", "new_york", "asia"))

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
        self._filter_simulator = _build_filter_simulator_helper(self)

        self._state_factory = _BacktestSignalState
        self._bars_to_evaluate = config.confidence.bars_to_evaluate

        # MarketStructureAnalyzer（结构化策略依赖）
        self._market_structure_analyzer: Any = None
        try:
            from src.market_structure import MarketStructureAnalyzer, MarketStructureConfig
            self._market_structure_analyzer = MarketStructureAnalyzer(
                market_service=_BacktestBarService(),
                config=MarketStructureConfig(lookback_bars=200),
            )
        except Exception:
            logger.debug("MarketStructureAnalyzer not available for backtest", exc_info=True)

        # Chandelier Exit 配置：从 signal.ini 加载（回测与实盘共享同一套出场参数）
        from src.trading.positions.exit_rules import ChandelierConfig as _CC
        from src.trading.positions.exit_rules import profile_from_aggression as _pfa
        from src.config.signal import get_signal_config as _get_sc
        try:
            _sc = _get_sc()
            self._chandelier_config = _CC(
                regime_aware=_sc.chandelier_regime_aware,
                fallback_profile=_pfa(_sc.chandelier_fallback_alpha),
                breakeven_enabled=_sc.chandelier_breakeven_enabled,
                breakeven_buffer_r=_sc.chandelier_breakeven_buffer_r,
                min_breakeven_buffer=_sc.chandelier_min_breakeven_buffer,
                signal_exit_enabled=_sc.chandelier_signal_exit_enabled,
                signal_exit_confirmation_bars=_sc.chandelier_signal_exit_confirmation_bars,
                timeout_bars=_sc.chandelier_timeout_bars,
                max_tp_r=_sc.chandelier_max_tp_r,
                enforce_r_floor=_sc.chandelier_enforce_r_floor,
                aggression_overrides=dict(_sc.chandelier_aggression_overrides),
                tf_trail_scale=dict(_sc.chandelier_tf_trail_scale),
            )
        except Exception:
            self._chandelier_config = _CC(regime_aware=True)

    def _reset_run_state(self) -> None:
        """重置每次 run() 的运行时状态，确保引擎可复用。"""
        config = self._config
        pos = config.position
        risk = config.risk
        ttp = config.trailing_tp

        self._signal_states: Dict[str, _BacktestSignalState] = {}
        if config.enable_state_machine:
            for s in self._target_strategies:
                self._signal_states[s] = _BacktestSignalState()
        self._signal_evaluations: List[SignalEvaluation] = []
        self._pending_evaluations: Dict[int, List[SignalEvaluation]] = {}
        self._recorded_evals: Set[Tuple[int, str]] = set()
        self._portfolio = PortfolioTracker(
            initial_balance=config.initial_balance,
            max_positions=risk.max_positions,
            commission_per_lot=risk.commission_per_lot,
            slippage_points=risk.slippage_points,
            contract_size=pos.contract_size,
            min_volume=pos.min_volume,
            max_volume=pos.max_volume,
            max_volume_per_order=risk.max_volume_per_order,
            max_volume_per_symbol=risk.max_volume_per_symbol,
            max_volume_per_day=risk.max_volume_per_day,
            daily_loss_limit_pct=risk.daily_loss_limit_pct,
            max_trades_per_day=risk.max_trades_per_day,
            max_trades_per_hour=risk.max_trades_per_hour,
            chandelier_config=self._chandelier_config,
            end_of_day_close_enabled=pos.end_of_day_close_enabled,
            end_of_day_close_hour_utc=pos.end_of_day_close_hour_utc,
            end_of_day_close_minute_utc=pos.end_of_day_close_minute_utc,
        )

    def run(self) -> BacktestResult:
        """执行回测主循环。支持多次调用（每次自动重置状态）。"""
        self._reset_run_state()
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
            from ..analysis.metrics import _empty_metrics

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
            self._config.confidence.enable_htf_alignment
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
                self._htf_indicator_data = _preload_htf_indicators_helper(
                    self,
                    symbol,
                    htf_list,
                    self._config.start_time,
                    self._config.end_time,
                )

        # 2. 如果没有预计算指标，一次性预计算全部（避免主循环内重复 pipeline 调用）
        if self._precomputed_indicators is not None:
            all_indicator_snapshots = self._precomputed_indicators
        else:
            all_indicator_snapshots = _precompute_all_indicators_helper(
                self,
                symbol, timeframe, all_bars, warmup_count
            )

        # 3. 逐 bar 回放
        equity_sample_interval = max(1, len(test_bars) // 500)  # 最多 500 个采样点

        # 上一 bar 的策略/投票方向（供 Chandelier 信号反转检查）
        _last_signal_directions: Dict[str, str] = {}

        for i in range(warmup_end, len(all_bars)):
            bar = all_bars[i]
            bar_index = i - warmup_end
            self._portfolio.observe_bar(bar)

            # 更新 MarketStructure bar 窗口（结构化策略依赖）
            if self._market_structure_analyzer is not None:
                ms_svc = self._market_structure_analyzer.market_service
                if hasattr(ms_svc, "update_window"):
                    ms_svc.update_window(all_bars[max(0, i - 400) : i + 1])

            # ��入 recent_bars 窗口（TrendlineTouch 等策略依赖，最多 80 根）
            self._recent_bars_window = all_bars[max(0, i - 80) : i + 1]

            # 直接取预计算的指标快照
            indicators = all_indicator_snapshots[i] if i < len(all_indicator_snapshots) else {}

            # 4. Regime 检测
            regime: Optional[RegimeType] = None
            soft_regime_dict: Optional[Dict[str, Any]] = None
            if indicators:
                regime, soft_regime_dict = _detect_regime_helper(self, indicators)

            # 提取当前 ATR 供 Chandelier Exit 使用
            _current_atr = 0.0
            atr_data = indicators.get("atr14") if indicators else None
            if isinstance(atr_data, dict):
                _current_atr = float(atr_data.get("atr", 0.0) or 0.0)

            # 5. 检查持仓出场（Chandelier Exit 4 规则，regime-aware）
            closed_trades = self._portfolio.check_exits(
                bar, bar_index, indicators,
                current_atr=_current_atr,
                current_regime=regime.value if regime else "",
                signal_directions=_last_signal_directions,
            )
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
                _check_pending_entries_helper(self, bar, bar_index, indicators, regime)

            # 5.5 回填待评估信号（N bars 后用当前价格回填结果）
            _backfill_evaluations_helper(self, bar_index, bar.close)

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
                    _record_evaluation_helper(
                        self,
                        bar=bar, bar_index=bar_index,
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
                self._htf_indicator_data = _lookup_htf_at_time_helper(self, bar.time)

            # 7. 策略评估
            decisions = _evaluate_strategies_helper(
                self,
                symbol,
                timeframe,
                indicators,
                regime,
                soft_regime_dict,
                bar_time=bar.time,
            )

            # 8. 记录信号评估 + 处理独立策略信号
            #    属于 voting group 的策略不单独 process_decision（与实盘一致）
            for decision in decisions:
                _record_evaluation_helper(
                    self,
                    bar=bar,
                    bar_index=bar_index,
                    strategy=decision.strategy,
                    action=decision.direction,
                    confidence=decision.confidence,
                    regime=regime.value,
                )
                if decision.strategy not in self._voting_group_members:
                    _process_decision_helper(
                        self, decision, bar, bar_index, indicators, regime
                    )

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
                                    _record_evaluation_helper(
                                        self,
                                        bar=bar, bar_index=bar_index,
                                        strategy=vote.strategy,
                                        action=vote.direction,
                                        confidence=vote.confidence,
                                        regime=regime.value,
                                    )
                                    _process_decision_helper(
                                        self,
                                        vote, bar, bar_index, indicators, regime
                                    )
                    # 单 consensus 模式
                    elif self._voting_engine is not None:
                        consensus = self._voting_engine.vote(
                            actionable, regime=regime, scope="confirmed"
                        )
                        if consensus is not None:
                            _record_evaluation_helper(
                                self,
                                bar=bar, bar_index=bar_index,
                                strategy=consensus.strategy,
                                action=consensus.direction,
                                confidence=consensus.confidence,
                                regime=regime.value,
                            )
                            _process_decision_helper(
                                self,
                                consensus, bar, bar_index, indicators, regime
                            )
                except Exception:
                    logger.warning(
                        "VotingEngine failed at bar %d", bar_index, exc_info=True
                    )

            # 9.5 收集本 bar 的策略/投票方向（供下一 bar 的 Chandelier 信号反转检查）
            _last_signal_directions = {}
            for d in decisions:
                if d.direction in ("buy", "sell"):
                    _last_signal_directions[d.strategy] = d.direction
            # 投票组结果也记录（持仓策略名是投票组名，如 "momentum_vote"）
            if actionable and self._voting_group_engines:
                for group_cfg, group_engine in self._voting_group_engines:
                    group_decisions = [
                        dd for dd in actionable if dd.strategy in group_cfg.strategies
                    ]
                    if group_decisions:
                        vote = group_engine.vote(
                            group_decisions, regime=regime, scope="confirmed",
                            exclude_composite=False,
                        )
                        if vote is not None and vote.direction in ("buy", "sell"):
                            _last_signal_directions[vote.strategy] = vote.direction

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
        _flush_pending_evaluations_helper(self, last_bar.close)

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

        # 蒙特卡洛排列检验
        mc_result = None
        mc = self._config.monte_carlo
        if mc.enabled and trades:
            from ..analysis.monte_carlo import MonteCarloConfig as MCRunConfig
            from ..analysis.monte_carlo import run_monte_carlo

            mc_result = run_monte_carlo(
                pnl_sequence=[t.pnl for t in trades],
                initial_balance=self._config.initial_balance,
                config=MCRunConfig(
                    enabled=True,
                    num_simulations=mc.simulations,
                    confidence_level=mc.confidence_level,
                    seed=mc.seed,
                ),
            ).to_dict()
            if mc_result.get("is_significant"):
                logger.info(
                    "Backtest %s: Monte Carlo SIGNIFICANT (Sharpe p=%.4f, PF p=%.4f)",
                    run_id, mc_result["sharpe_p_value"], mc_result["profit_factor_p_value"],
                )
            else:
                logger.warning(
                    "Backtest %s: Monte Carlo NOT significant (Sharpe p=%.4f, "
                    "real=%.4f vs random 95th=%.4f)",
                    run_id, mc_result["sharpe_p_value"],
                    mc_result["real_sharpe"], mc_result["random_sharpe_95th"],
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
            monte_carlo_result=mc_result,
        )
