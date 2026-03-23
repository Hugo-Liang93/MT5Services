"""Walk-Forward 验证框架：滚动窗口前推回测，检测过拟合。

将总时间区间拆分为多个 train/test 窗口，在每个 train 窗口上
（可选）运行参数优化，然后在 test 窗口上验证样本外表现。
通过对比 IS/OOS 指标比率和 OOS 盈利一致性评估策略稳健性。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.signals.evaluation.regime import MarketRegimeDetector
from src.signals.service import SignalModule

from .data_loader import HistoricalDataLoader
from .engine import BacktestEngine
from .metrics import _empty_metrics, compute_metrics
from .models import BacktestConfig, BacktestMetrics, BacktestResult, ParameterSpace
from .optimizer import ParameterOptimizer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WalkForwardConfig:
    """Walk-Forward 验证配置。

    Attributes:
        total_start_time: 总时间区间起点
        total_end_time: 总时间区间终点
        train_ratio: 每个窗口中训练集占比（0 < ratio < 1）
        n_splits: 滚动窗口数量
        anchored: True = 扩展窗口（训练起点固定），False = 滚动窗口
        optimization_metric: 训练窗口内优化目标指标名
        param_space: 参数搜索空间（提供时在每个训练窗口运行优化器）
        base_config: 模板回测配置（start_time/end_time 会被每个窗口覆盖）
    """

    total_start_time: datetime
    total_end_time: datetime
    base_config: BacktestConfig
    train_ratio: float = 0.7
    n_splits: int = 5
    anchored: bool = False
    optimization_metric: str = "sharpe_ratio"
    param_space: Optional[ParameterSpace] = None


@dataclass(frozen=True)
class WalkForwardSplit:
    """单个 Walk-Forward 窗口的结果。

    Attributes:
        split_index: 窗口序号（从 0 开始）
        train_start: 训练集起始时间
        train_end: 训练集结束时间
        test_start: 测试集起始时间
        test_end: 测试集结束时间
        best_params: 训练窗口选出的最优参数（无优化时为 base_config 参数）
        in_sample_result: 训练集回测结果
        out_of_sample_result: 测试集回测结果
    """

    split_index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    best_params: Dict[str, Any]
    in_sample_result: BacktestResult
    out_of_sample_result: BacktestResult


@dataclass
class WalkForwardResult:
    """Walk-Forward 验证汇总结果。

    Attributes:
        splits: 每个窗口的详细结果
        aggregate_metrics: 合并所有 OOS 交易计算的统计指标
        overfitting_ratio: avg(IS_metric) / avg(OOS_metric)，> 2.0 表示可能过拟合
        consistency_rate: OOS 窗口盈利的比例
        config: 本次验证使用的配置
    """

    splits: List[WalkForwardSplit]
    aggregate_metrics: BacktestMetrics
    overfitting_ratio: float
    consistency_rate: float
    config: WalkForwardConfig


# 进度回调签名：(当前窗口序号, 总窗口数, 阶段描述)
WalkForwardProgressCallback = Callable[[int, int, str], None]


class WalkForwardValidator:
    """Walk-Forward 验证器：编排多窗口的训练-测试前推流程。

    对每个窗口：
    1. 在训练集上运行参数优化（或直接使用基础参数）
    2. 用最优参数在测试集上回测
    3. 汇总所有 OOS 结果评估策略稳健性
    """

    def __init__(
        self,
        config: WalkForwardConfig,
        data_loader: HistoricalDataLoader,
        signal_module_factory: Callable[[Dict[str, Any]], SignalModule],
        indicator_pipeline: Any,  # OptimizedPipeline
        regime_detector: Optional[MarketRegimeDetector] = None,
        voting_engine: Optional[Any] = None,
    ) -> None:
        """初始化 Walk-Forward 验证器。

        Args:
            config: Walk-Forward 配置
            data_loader: 历史数据加载器
            signal_module_factory: 接收策略参数覆盖、返回独立 SignalModule 的工厂函数
            indicator_pipeline: 指标计算管线
            regime_detector: Regime 检测器（可选）
            voting_engine: 投票引擎（可选）
        """
        self._config = config
        self._data_loader = data_loader
        self._signal_module_factory = signal_module_factory
        self._pipeline = indicator_pipeline
        self._regime_detector = regime_detector or MarketRegimeDetector()
        self._voting_engine = voting_engine

    def run(
        self,
        progress_callback: Optional[WalkForwardProgressCallback] = None,
    ) -> WalkForwardResult:
        """执行 Walk-Forward 验证。

        Args:
            progress_callback: 进度回调，签名 (当前序号, 总数, 阶段描述)

        Returns:
            WalkForwardResult 汇总结果
        """
        time_splits = self._generate_splits()
        n_splits = len(time_splits)
        logger.info(
            "Walk-Forward validation: %d splits, train_ratio=%.2f, anchored=%s",
            n_splits,
            self._config.train_ratio,
            self._config.anchored,
        )

        splits: List[WalkForwardSplit] = []

        for i, (train_start, train_end, test_start, test_end) in enumerate(time_splits):
            logger.info(
                "Split %d/%d: train=[%s ~ %s], test=[%s ~ %s]",
                i + 1,
                n_splits,
                train_start.isoformat(),
                train_end.isoformat(),
                test_start.isoformat(),
                test_end.isoformat(),
            )

            if progress_callback:
                progress_callback(i + 1, n_splits, "training")

            # ── 训练阶段 ──────────────────────────────────────────
            best_params: Dict[str, Any] = dict(self._config.base_config.strategy_params)

            if self._config.param_space is not None:
                # 有参数空间：在训练集上运行优化器
                train_config = replace(
                    self._config.base_config,
                    start_time=train_start,
                    end_time=train_end,
                )
                optimizer = ParameterOptimizer(
                    base_config=train_config,
                    param_space=self._config.param_space,
                    data_loader=self._data_loader,
                    indicator_pipeline=self._pipeline,
                    signal_module_factory=self._signal_module_factory,
                    regime_detector=self._regime_detector,
                    voting_engine=self._voting_engine,
                    sort_metric=self._config.optimization_metric,
                )
                opt_results = optimizer.run()
                if opt_results:
                    best_params = dict(opt_results[0].param_set)
                    logger.info(
                        "Split %d: optimizer selected params=%s (metric=%.4f)",
                        i + 1,
                        best_params,
                        getattr(
                            opt_results[0].metrics,
                            self._config.optimization_metric,
                            0.0,
                        ),
                    )

            # ── 用最优参数在训练集上回测（IS 结果）────────────────
            in_sample_result = self._run_backtest(
                train_start, train_end, best_params
            )

            # ── 用最优参数在测试集上回测（OOS 结果）───────────────
            if progress_callback:
                progress_callback(i + 1, n_splits, "testing")

            out_of_sample_result = self._run_backtest(
                test_start, test_end, best_params
            )

            split = WalkForwardSplit(
                split_index=i,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                best_params=best_params,
                in_sample_result=in_sample_result,
                out_of_sample_result=out_of_sample_result,
            )
            splits.append(split)

            logger.info(
                "Split %d result: IS sharpe=%.4f pnl=%.2f | OOS sharpe=%.4f pnl=%.2f",
                i + 1,
                in_sample_result.metrics.sharpe_ratio,
                in_sample_result.metrics.total_pnl,
                out_of_sample_result.metrics.sharpe_ratio,
                out_of_sample_result.metrics.total_pnl,
            )

        # ── 汇总 OOS 结果 ────────────────────────────────────────
        aggregate_metrics = self._compute_aggregate_metrics(splits)
        overfitting_ratio = self._compute_overfitting_ratio(splits)
        consistency_rate = self._compute_consistency_rate(splits)

        logger.info(
            "Walk-Forward complete: overfitting_ratio=%.4f, consistency_rate=%.2f%%",
            overfitting_ratio,
            consistency_rate * 100,
        )

        return WalkForwardResult(
            splits=splits,
            aggregate_metrics=aggregate_metrics,
            overfitting_ratio=overfitting_ratio,
            consistency_rate=consistency_rate,
            config=self._config,
        )

    def _generate_splits(
        self,
    ) -> List[Tuple[datetime, datetime, datetime, datetime]]:
        """生成 train/test 时间窗口切分。

        滚动窗口模式（anchored=False）：
            每个窗口大小固定，窗口按步长滑动。

        扩展窗口模式（anchored=True）：
            训练集起点固定为 total_start_time，训练集终点随窗口递增。

        Returns:
            列表，每个元素为 (train_start, train_end, test_start, test_end)
        """
        total_start = self._config.total_start_time
        total_end = self._config.total_end_time
        n_splits = self._config.n_splits
        train_ratio = self._config.train_ratio

        total_seconds = (total_end - total_start).total_seconds()

        if self._config.anchored:
            # 扩展窗口：训练起点固定，每个 split 的 test 窗口等分剩余区间
            # 最小训练集 = train_ratio * 总时长 / n_splits 的首个窗口
            # 测试窗口大小 = (1 - train_ratio) * 总时长 / n_splits
            test_duration = total_seconds * (1.0 - train_ratio) / n_splits
            # 首个训练集占总时长的 train_ratio 部分
            first_train_duration = total_seconds * train_ratio

            splits: List[Tuple[datetime, datetime, datetime, datetime]] = []
            for i in range(n_splits):
                train_start_t = total_start
                # 训练集终点 = 首个训练集终点 + i 个 test 窗口的长度
                train_end_t = total_start + timedelta(
                    seconds=first_train_duration + i * test_duration
                )
                test_start_t = train_end_t
                test_end_t = test_start_t + timedelta(seconds=test_duration)
                # 确保不超出总区间
                if test_end_t > total_end:
                    test_end_t = total_end
                if test_start_t >= total_end:
                    break
                splits.append((train_start_t, train_end_t, test_start_t, test_end_t))
            return splits
        else:
            # 滚动窗口：每个窗口大小相同，按步长滑动
            # 单个窗口总长 = 总时长 / (n_splits * (1 - train_ratio) + train_ratio)
            # 步长 = 窗口总长 * (1 - train_ratio) = test 窗口大小
            window_seconds = total_seconds / (
                n_splits * (1.0 - train_ratio) + train_ratio
            )
            train_seconds = window_seconds * train_ratio
            test_seconds = window_seconds * (1.0 - train_ratio)

            splits = []
            for i in range(n_splits):
                offset = i * test_seconds
                train_start_t = total_start + timedelta(seconds=offset)
                train_end_t = train_start_t + timedelta(seconds=train_seconds)
                test_start_t = train_end_t
                test_end_t = test_start_t + timedelta(seconds=test_seconds)
                if test_end_t > total_end:
                    test_end_t = total_end
                if test_start_t >= total_end:
                    break
                splits.append((train_start_t, train_end_t, test_start_t, test_end_t))
            return splits

    def _run_backtest(
        self,
        start_time: datetime,
        end_time: datetime,
        strategy_params: Dict[str, Any],
    ) -> BacktestResult:
        """在指定时间窗口上运行单次回测。

        Args:
            start_time: 窗口起始时间
            end_time: 窗口结束时间
            strategy_params: 策略参数覆盖

        Returns:
            BacktestResult
        """
        config = replace(
            self._config.base_config,
            start_time=start_time,
            end_time=end_time,
            strategy_params=strategy_params,
        )
        signal_module = self._signal_module_factory(strategy_params)
        engine = BacktestEngine(
            config=config,
            data_loader=self._data_loader,
            signal_module=signal_module,
            indicator_pipeline=self._pipeline,
            regime_detector=self._regime_detector,
            voting_engine=self._voting_engine,
        )
        return engine.run()

    def _compute_aggregate_metrics(
        self,
        splits: List[WalkForwardSplit],
    ) -> BacktestMetrics:
        """合并所有 OOS 窗口的交易，计算统一指标。"""
        all_oos_trades = []
        all_oos_equity: List[float] = []
        initial_balance = self._config.base_config.initial_balance

        for split in splits:
            all_oos_trades.extend(split.out_of_sample_result.trades)
            # equity_curve 是 List[Tuple[datetime, float]]，提取数值部分
            for _, value in split.out_of_sample_result.equity_curve:
                all_oos_equity.append(value)

        if not all_oos_trades:
            return _empty_metrics()

        if not all_oos_equity:
            all_oos_equity = [initial_balance]

        return compute_metrics(all_oos_trades, initial_balance, all_oos_equity)

    def _compute_overfitting_ratio(
        self,
        splits: List[WalkForwardSplit],
    ) -> float:
        """计算过拟合比率：avg(IS_metric) / avg(OOS_metric)。

        比率含义：
        - ~1.0：IS 和 OOS 表现接近，策略稳健
        - > 2.0：IS 远优于 OOS，可能过拟合
        - < 1.0：OOS 优于 IS（罕见但可能，说明策略在新数据上表现更好）

        当 OOS 指标均值为 0 或负数时返回 float('inf') 表示严重过拟合。
        """
        if not splits:
            return 0.0

        metric_name = self._config.optimization_metric
        is_values = [
            getattr(s.in_sample_result.metrics, metric_name, 0.0) for s in splits
        ]
        oos_values = [
            getattr(s.out_of_sample_result.metrics, metric_name, 0.0) for s in splits
        ]

        avg_is = sum(is_values) / len(is_values) if is_values else 0.0
        avg_oos = sum(oos_values) / len(oos_values) if oos_values else 0.0

        if avg_oos <= 0.0:
            if avg_is <= 0.0:
                return 1.0  # 双方都不盈利，无过拟合信号
            return float("inf")

        return avg_is / avg_oos

    def _compute_consistency_rate(
        self,
        splits: List[WalkForwardSplit],
    ) -> float:
        """计算 OOS 盈利一致性：盈利窗口数 / 总窗口数。"""
        if not splits:
            return 0.0

        profitable_count = sum(
            1 for s in splits if s.out_of_sample_result.metrics.total_pnl > 0
        )
        return profitable_count / len(splits)
