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
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from src.clients.mt5_market import OHLC
from src.signals.confidence import apply_htf_alignment, apply_intrabar_decay
from src.signals.evaluation.regime import MarketRegimeDetector, RegimeType
from src.signals.models import SignalDecision
from src.signals.service import SignalModule
from src.trading.pending_entry import PendingEntryConfig, compute_entry_zone
from src.trading.sizing import compute_trade_params

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
        voting_engine: Optional[Any] = None,  # StrategyVotingEngine
        # 置信度后处理（复用实盘 SignalRuntime 管线）
        intrabar_confidence_decay: float = 0.85,
        htf_direction_fn: Optional[Callable[[str, str], Optional[str]]] = None,
        htf_alignment_boost: float = 1.10,
        htf_conflict_penalty: float = 0.70,
        # HTF 指标预计算数据（从更高时间框架加载）
        htf_indicator_data: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    ) -> None:
        self._config = config
        self._data_loader = data_loader
        self._signal_module = signal_module
        self._pipeline = indicator_pipeline
        self._regime_detector = regime_detector or MarketRegimeDetector()
        self._voting_engine = voting_engine

        # 置信度后处理参数（与实盘 SignalRuntime 相同）
        self._intrabar_confidence_decay = intrabar_confidence_decay
        self._htf_direction_fn = htf_direction_fn
        self._htf_alignment_boost = htf_alignment_boost
        self._htf_conflict_penalty = htf_conflict_penalty
        # HTF 指标：{timeframe: {indicator_name: {field: value}}}
        self._htf_indicator_data = htf_indicator_data or {}

        self._portfolio = PortfolioTracker(
            initial_balance=config.initial_balance,
            max_positions=config.max_positions,
            commission_per_lot=config.commission_per_lot,
            slippage_points=config.slippage_points,
            contract_size=config.contract_size,
            trailing_atr_multiplier=config.trailing_atr_multiplier,
            breakeven_atr_threshold=config.breakeven_atr_threshold,
            end_of_day_close_enabled=config.end_of_day_close_enabled,
            end_of_day_close_hour_utc=config.end_of_day_close_hour_utc,
            end_of_day_close_minute_utc=config.end_of_day_close_minute_utc,
        )

        # Pending Entry 配置（复用实盘 compute_entry_zone 纯函数）
        self._pending_entry_enabled = config.enable_pending_entry
        self._pending_entry_config = PendingEntryConfig(
            pullback_atr_factor=config.pending_entry_pullback_atr_factor,
            chase_atr_factor=config.pending_entry_chase_atr_factor,
            momentum_atr_factor=config.pending_entry_momentum_atr_factor,
            symmetric_atr_factor=config.pending_entry_symmetric_atr_factor,
        )
        # 挂起的入场意图：{signal_key: (decision, entry_low, entry_high, expiry_bar)}
        self._pending_entries: Dict[str, Tuple[SignalDecision, float, float, int]] = {}

        # 确定目标策略列表
        if config.strategies:
            self._target_strategies = config.strategies
        else:
            self._target_strategies = self._signal_module.list_strategies()

        # 收集所有目标策略需要的指标名
        self._required_indicators: List[str] = []
        seen: Set[str] = set()
        for s in self._target_strategies:
            for ind in self._signal_module.strategy_requirements(s):
                if ind not in seen:
                    seen.add(ind)
                    self._required_indicators.append(ind)

        # 构建过滤器模拟器
        self._filter_simulator = self._build_filter_simulator()

        # 信号评估记录（用于回测质量分析 + 数据落表）
        self._signal_evaluations: List[SignalEvaluation] = []
        # 待回填的 pending 评估：{bar_index: [SignalEvaluation]}
        self._pending_evaluations: Dict[int, List[SignalEvaluation]] = {}
        self._bars_to_evaluate = 5  # N bars 后回填
        # 去重：已记录的 (bar_index, strategy) 组合
        self._recorded_evals: Set[Tuple[int, str]] = set()

    def _build_filter_simulator(self) -> BacktestFilterSimulator:
        """从 BacktestConfig 构建过滤器模拟器。"""
        filter_config = BacktestFilterConfig(
            enabled=self._config.enable_filters,
            session_filter_enabled=self._config.filter_session_enabled,
            allowed_sessions=self._config.filter_allowed_sessions,
            session_transition_enabled=self._config.filter_session_transition_enabled,
            session_transition_cooldown_minutes=self._config.filter_session_transition_cooldown,
            volatility_filter_enabled=self._config.filter_volatility_enabled,
            volatility_spike_multiplier=self._config.filter_volatility_spike_multiplier,
            spread_filter_enabled=self._config.filter_spread_enabled,
            max_spread_points=self._config.filter_max_spread_points,
        )
        return BacktestFilterSimulator(filter_config)

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

        # 2. 逐 bar 回放
        equity_sample_interval = max(1, len(test_bars) // 500)  # 最多 500 个采样点

        for i in range(warmup_end, len(all_bars)):
            bar = all_bars[i]
            bar_index = i - warmup_end

            # 滚动窗口：最近 warmup_count + 1 根 bar
            window_start = max(0, i - warmup_count)
            window = all_bars[window_start : i + 1]

            # 3. 计算指标
            indicators = self._compute_indicators(symbol, timeframe, window)

            # 4. Regime 检测
            regime: Optional[RegimeType] = None
            soft_regime_dict: Optional[Dict[str, Any]] = None
            if indicators:
                regime, soft_regime_dict = self._detect_regime(indicators)

            # 5. 检查持仓 SL/TP
            self._portfolio.check_exits(bar, bar_index)

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

            # 7. 策略评估
            decisions = self._evaluate_strategies(
                symbol, timeframe, indicators, regime, soft_regime_dict
            )

            # 8. 记录信号评估 + 处理信号
            for decision in decisions:
                self._record_evaluation(
                    bar=bar,
                    bar_index=bar_index,
                    strategy=decision.strategy,
                    action=decision.action,
                    confidence=decision.confidence,
                    regime=regime.value,
                )
                self._process_decision(decision, bar, bar_index, indicators, regime)

            # 9. 投票引擎
            if self._voting_engine is not None and decisions:
                actionable = [d for d in decisions if d.action in ("buy", "sell")]
                if actionable:
                    consensus = self._voting_engine.vote(actionable, regime, "confirmed")
                    if consensus is not None:
                        self._record_evaluation(
                            bar=bar,
                            bar_index=bar_index,
                            strategy=consensus.strategy,
                            action=consensus.action,
                            confidence=consensus.confidence,
                            regime=regime.value,
                        )
                        self._process_decision(
                            consensus, bar, bar_index, indicators, regime
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

    def _compute_indicators(
        self,
        symbol: str,
        timeframe: str,
        bars: List[OHLC],
    ) -> Dict[str, Dict[str, Any]]:
        """使用生产 Pipeline 计算指标。"""
        if len(bars) < 2:
            return {}
        try:
            results = self._pipeline.compute(
                symbol, timeframe, bars, self._required_indicators
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
    ) -> Tuple[RegimeType, Optional[Dict[str, Any]]]:
        """检测市场 Regime。"""
        regime = self._regime_detector.detect(indicators)
        soft_regime_dict: Optional[Dict[str, Any]] = None
        try:
            soft_result = self._regime_detector.detect_soft(indicators)
            if soft_result is not None:
                soft_regime_dict = soft_result.to_dict()
        except Exception:
            pass
        return regime, soft_regime_dict

    def _evaluate_strategies(
        self,
        symbol: str,
        timeframe: str,
        indicators: Dict[str, Dict[str, Any]],
        regime: RegimeType,
        soft_regime_dict: Optional[Dict[str, Any]],
        scope: str = "confirmed",
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
        # P1: enable_regime_affinity=False 时绕过 Regime 亲和度修正
        if not self._config.enable_regime_affinity:
            metadata["_pre_computed_affinity"] = 1.0

        decisions: List[SignalDecision] = []
        for strategy_name in self._target_strategies:
            try:
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

                # ── 置信度后处理（复用实盘 SignalRuntime 管线）──
                # 1. Intrabar 衰减
                decision = apply_intrabar_decay(
                    decision, scope, self._intrabar_confidence_decay
                )
                # 2. HTF 方向对齐修正（受 enable_htf_alignment 控制）
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

    def _process_decision(
        self,
        decision: SignalDecision,
        bar: OHLC,
        bar_index: int,
        indicators: Dict[str, Dict[str, Any]],
        regime: RegimeType,
    ) -> None:
        """处理单个信号决策：开仓或反向关仓。"""
        if decision.action not in ("buy", "sell"):
            return
        if decision.confidence < self._config.min_confidence:
            return

        # 检查是否有反向持仓需要先关闭
        opposite = "sell" if decision.action == "buy" else "buy"
        for pos in list(self._portfolio._open_positions):
            if pos.strategy == decision.strategy and pos.action == opposite:
                self._portfolio.close_by_signal(decision.strategy, bar, bar_index)
                break

        # 获取 ATR 值用于 sizing
        atr_data = indicators.get("atr14", {})
        atr_value = atr_data.get("atr", 0.0)
        if atr_value <= 0:
            return

        # Pending Entry 模拟：不立即开仓，等后续 bar 价格落入区间
        if self._pending_entry_enabled:
            category = decision.metadata.get("category", "trend")
            from src.trading.pending_entry import _CATEGORY_ZONE_MODE

            zone_mode = _CATEGORY_ZONE_MODE.get(category, "pullback")
            entry_low, entry_high = compute_entry_zone(
                action=decision.action,
                close_price=bar.close,
                atr=atr_value,
                zone_mode=zone_mode,
                config=self._pending_entry_config,
                strategy_name=decision.strategy,
            )
            # 默认 2 bars 超时
            expiry_bar = bar_index + 2
            key = f"{decision.strategy}_{decision.action}"
            self._pending_entries[key] = (decision, entry_low, entry_high, expiry_bar)
            return

        self._execute_entry(decision, bar, bar_index, atr_value, regime)

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
                    decision.strategy, decision.action, bar_index, expiry_bar,
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
                        decision, bar, bar_index, atr_value, regime
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
    ) -> None:
        """执行实际开仓。"""
        try:
            trade_params = compute_trade_params(
                action=decision.action,
                current_price=bar.close,
                atr_value=atr_value,
                account_balance=self._portfolio.current_balance,
                timeframe=self._config.timeframe,
                risk_percent=self._config.risk_percent,
                contract_size=self._config.contract_size,
            )
        except ValueError as e:
            logger.debug(
                "Trade params computation failed for %s %s: %s",
                decision.strategy, decision.action, e,
            )
            return

        self._portfolio.open_position(
            strategy=decision.strategy,
            action=decision.action,
            bar=bar,
            trade_params=trade_params,
            regime=regime.value,
            confidence=decision.confidence,
            bar_index=bar_index,
            atr_at_entry=atr_value,
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
        dedup_key = (bar_index, strategy)
        if dedup_key in self._recorded_evals:
            return
        self._recorded_evals.add(dedup_key)

        eval_record = SignalEvaluation(
            bar_time=bar.time,
            strategy=strategy,
            action=action,
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

    def _backfill_evaluations(self, current_bar_index: int, current_price: float) -> None:
        """回填已到期的 pending 信号评估（N bars 后用当前价格计算盈亏）。"""
        if current_bar_index not in self._pending_evaluations:
            return

        pending_list = self._pending_evaluations.pop(current_bar_index)
        for ev in pending_list:
            if ev.action == "buy":
                pnl_pct = (
                    (current_price - ev.price_at_signal) / ev.price_at_signal * 100
                )
                won = current_price > ev.price_at_signal
            else:
                pnl_pct = (
                    (ev.price_at_signal - current_price) / ev.price_at_signal * 100
                )
                won = current_price < ev.price_at_signal

            filled = SignalEvaluation(
                bar_time=ev.bar_time,
                strategy=ev.strategy,
                action=ev.action,
                confidence=ev.confidence,
                regime=ev.regime,
                price_at_signal=ev.price_at_signal,
                price_after_n_bars=current_price,
                bars_to_evaluate=ev.bars_to_evaluate,
                won=won,
                pnl_pct=round(pnl_pct, 4),
                filtered=ev.filtered,
                filter_reason=ev.filter_reason,
            )
            self._signal_evaluations.append(filled)

    def _flush_pending_evaluations(self, last_price: float) -> None:
        """回测结束时回填所有未到期的 pending 评估。"""
        for _target_index, pending_list in sorted(self._pending_evaluations.items()):
            for ev in pending_list:
                if ev.action == "buy":
                    pnl_pct = (
                        (last_price - ev.price_at_signal) / ev.price_at_signal * 100
                    )
                    won = last_price > ev.price_at_signal
                else:
                    pnl_pct = (
                        (ev.price_at_signal - last_price) / ev.price_at_signal * 100
                    )
                    won = last_price < ev.price_at_signal

                filled = SignalEvaluation(
                    bar_time=ev.bar_time,
                    strategy=ev.strategy,
                    action=ev.action,
                    confidence=ev.confidence,
                    regime=ev.regime,
                    price_at_signal=ev.price_at_signal,
                    price_after_n_bars=last_price,
                    bars_to_evaluate=ev.bars_to_evaluate,
                    won=won,
                    pnl_pct=round(pnl_pct, 4),
                    filtered=ev.filtered,
                    filter_reason=ev.filter_reason,
                )
                self._signal_evaluations.append(filled)
        self._pending_evaluations.clear()
