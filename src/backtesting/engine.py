"""回测引擎核心：逐 bar 回放历史数据，复用生产指标和策略组件。"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.clients.mt5_market import OHLC
from src.signals.evaluation.regime import MarketRegimeDetector, RegimeType
from src.signals.models import SignalDecision
from src.signals.service import SignalModule
from src.trading.sizing import compute_trade_params

from .data_loader import HistoricalDataLoader
from .metrics import compute_metrics, compute_metrics_grouped
from .models import BacktestConfig, BacktestResult, generate_run_id
from .portfolio import PortfolioTracker

logger = logging.getLogger(__name__)


class BacktestEngine:
    """回测引擎：逐 bar 回放历史数据，复用生产指标和策略组件。

    核心设计：
    - 直接复用 OptimizedPipeline.compute() 计算指标
    - 直接复用 SignalModule.evaluate() 评估策略（含完整置信度管线）
    - persist=False 避免写入生产 DB
    - 滚动窗口传入 bar 数据（窗口大小 = warmup_bars）
    """

    def __init__(
        self,
        config: BacktestConfig,
        data_loader: HistoricalDataLoader,
        signal_module: SignalModule,
        indicator_pipeline: Any,  # OptimizedPipeline
        regime_detector: Optional[MarketRegimeDetector] = None,
        voting_engine: Optional[Any] = None,  # StrategyVotingEngine
    ) -> None:
        self._config = config
        self._data_loader = data_loader
        self._signal_module = signal_module
        self._pipeline = indicator_pipeline
        self._regime_detector = regime_detector or MarketRegimeDetector()
        self._voting_engine = voting_engine

        self._portfolio = PortfolioTracker(
            initial_balance=config.initial_balance,
            max_positions=config.max_positions,
            commission_per_lot=config.commission_per_lot,
            slippage_points=config.slippage_points,
            contract_size=config.contract_size,
        )

        # 确定目标策略列表
        if config.strategies:
            self._target_strategies = config.strategies
        else:
            self._target_strategies = self._signal_module.list_strategies()

        # 收集所有目标策略需要的指标名
        self._required_indicators: List[str] = []
        seen: set[str] = set()
        for s in self._target_strategies:
            for ind in self._signal_module.strategy_requirements(s):
                if ind not in seen:
                    seen.add(ind)
                    self._required_indicators.append(ind)

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
            if not indicators:
                continue

            # 4. Regime 检测
            regime, soft_regime_dict = self._detect_regime(indicators)

            # 5. 检查持仓 SL/TP
            self._portfolio.check_exits(bar, bar_index)

            # 6. 策略评估
            decisions = self._evaluate_strategies(
                symbol, timeframe, indicators, regime, soft_regime_dict
            )

            # 7. 处理信号
            for decision in decisions:
                self._process_decision(decision, bar, bar_index, indicators, regime)

            # 8. 投票引擎
            if self._voting_engine is not None and decisions:
                actionable = [d for d in decisions if d.action in ("buy", "sell")]
                if actionable:
                    consensus = self._voting_engine.vote(actionable, regime, "confirmed")
                    if consensus is not None:
                        self._process_decision(
                            consensus, bar, bar_index, indicators, regime
                        )

            # 9. 记录资金曲线（采样）
            if bar_index % equity_sample_interval == 0:
                self._portfolio.record_equity(bar)

        # 10. 最终资金快照
        if test_bars:
            self._portfolio.record_equity(test_bars[-1])

        # 11. 强制平仓剩余持仓
        last_bar = all_bars[-1]
        last_index = len(all_bars) - 1 - warmup_end
        self._portfolio.close_all(last_bar, last_index)

        # 12. 汇总结果
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

        logger.info(
            "Backtest %s completed: %d trades, win_rate=%.2f%%, PnL=%.2f, elapsed=%dms",
            run_id,
            metrics.total_trades,
            metrics.win_rate * 100,
            metrics.total_pnl,
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
        except Exception:
            logger.debug("Indicator computation failed", exc_info=True)
            return {}

    def _detect_regime(
        self, indicators: Dict[str, Dict[str, Any]]
    ) -> tuple[RegimeType, Optional[Dict[str, Any]]]:
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
    ) -> List[SignalDecision]:
        """评估所有目标策略。"""
        metadata: Dict[str, Any] = {"_regime": regime.value}
        if soft_regime_dict is not None:
            metadata["_soft_regime"] = soft_regime_dict

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
                    htf_indicators={},
                )
                decisions.append(decision)
            except Exception:
                logger.debug(
                    "Strategy %s evaluation failed", strategy_name, exc_info=True
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
        except ValueError:
            return

        self._portfolio.open_position(
            strategy=decision.strategy,
            action=decision.action,
            bar=bar,
            trade_params=trade_params,
            regime=regime.value,
            confidence=decision.confidence,
            bar_index=bar_index,
        )
