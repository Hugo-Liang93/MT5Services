"""参数优化器：网格搜索与随机搜索编排。"""

from __future__ import annotations

import logging
import random
from dataclasses import replace
from itertools import product
from typing import Any, Callable, Dict, List, Optional

from src.signals.evaluation.regime import MarketRegimeDetector, RegimeType
from src.signals.service import SignalModule
from src.signals.strategies.breakout import MultiTimeframeConfirmStrategy
from src.signals.strategies.registry import build_composite_strategies

from .data_loader import CachedDataLoader, HistoricalDataLoader
from .engine import BacktestEngine
from .models import BacktestConfig, BacktestResult, ParameterSpace

logger = logging.getLogger(__name__)

# 进度回调签名：(current_index, total_count, latest_result)
ProgressCallback = Callable[[int, int, BacktestResult], None]


class ParameterOptimizer:
    """参数优化器：编排多组参数的回测运行。

    每次回测构建独立的 SignalModule 实例（避免状态污染）。
    参数格式与 signal.ini [strategy_params] 完全一致（双下划线格式）。
    """

    def __init__(
        self,
        base_config: BacktestConfig,
        param_space: ParameterSpace,
        data_loader: HistoricalDataLoader,
        indicator_pipeline: Any,  # OptimizedPipeline
        signal_module_factory: Callable[[Dict[str, Any]], SignalModule],
        regime_detector: Optional[MarketRegimeDetector] = None,
        voting_engine: Optional[Any] = None,
        sort_metric: str = "sharpe_ratio",
    ) -> None:
        self._base_config = base_config
        self._param_space = param_space
        self._data_loader = data_loader
        self._pipeline = indicator_pipeline
        self._signal_module_factory = signal_module_factory
        self._regime_detector = regime_detector or MarketRegimeDetector()
        self._voting_engine = voting_engine
        self._sort_metric = sort_metric

    def run(
        self,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> List[BacktestResult]:
        """执行参数优化。

        性能优化：
        1. 一次性预加载数据（warmup + test bars），后续迭代使用 CachedDataLoader
        2. 一次性预计算指标快照，所有参数组合复用（指标值不随策略参数变化）

        Returns:
            按 sort_metric 降序排列的回测结果列表。
        """
        combinations = self._generate_combinations()
        total = len(combinations)
        logger.info(
            "Parameter optimization: %d combinations (%s mode)",
            total,
            self._param_space.search_mode,
        )

        # ── 性能优化：一次性预加载数据 ──────────────────────────────
        warmup_bars = self._data_loader.preload_warmup_bars(
            self._base_config.symbol,
            self._base_config.timeframe,
            self._base_config.start_time,
            self._base_config.warmup_bars,
        )
        test_bars = self._data_loader.load_all_bars(
            self._base_config.symbol,
            self._base_config.timeframe,
            self._base_config.start_time,
            self._base_config.end_time,
        )
        cached_loader = CachedDataLoader(warmup_bars, test_bars)

        # ── 性能优化：一次性预计算指标 ──────────────────────────────
        # 指标值只依赖 bar 数据，不随策略参数变化，可安全复用
        precomputed: Optional[List[Dict[str, Any]]] = None
        if test_bars:
            logger.info(
                "Pre-computing indicators for %d+%d bars...",
                len(warmup_bars),
                len(test_bars),
            )
            # 借助临时 engine 预计算指标
            temp_module = self._signal_module_factory(
                combinations[0] if combinations else {}
            )
            temp_engine = BacktestEngine(
                config=self._base_config,
                data_loader=cached_loader,
                signal_module=temp_module,
                indicator_pipeline=self._pipeline,
                regime_detector=self._regime_detector,
            )
            all_bars = warmup_bars + test_bars
            precomputed = temp_engine._precompute_all_indicators(
                self._base_config.symbol,
                self._base_config.timeframe,
                all_bars,
                self._base_config.warmup_bars,
            )

        # 持仓参数键集合（用于从组合中分离策略参数和持仓参数）
        position_param_keys = set(self._param_space.position_params.keys())

        results: List[BacktestResult] = []
        for i, param_set in enumerate(combinations):
            # 分离策略参数和持仓参数
            strategy_params = {
                k: v for k, v in param_set.items() if k not in position_param_keys
            }
            position_overrides = {
                k: v for k, v in param_set.items() if k in position_param_keys
            }

            # 构建带参数覆盖的配置
            config = replace(
                self._base_config, strategy_params=strategy_params, **position_overrides
            )

            # 构建独立的 SignalModule
            signal_module = self._signal_module_factory(strategy_params)

            # 创建引擎并运行（复用缓存数据和预计算指标）
            engine = BacktestEngine(
                config=config,
                data_loader=cached_loader,
                signal_module=signal_module,
                indicator_pipeline=self._pipeline,
                regime_detector=self._regime_detector,
                voting_engine=self._voting_engine,
                precomputed_indicators=precomputed,
            )
            result = engine.run()
            results.append(result)

            if progress_callback:
                progress_callback(i + 1, total, result)

            logger.info(
                "Optimization [%d/%d]: params=%s → sharpe=%.4f, win_rate=%.2f%%, pnl=%.2f",
                i + 1,
                total,
                param_set,
                result.metrics.sharpe_ratio,
                result.metrics.win_rate * 100,
                result.metrics.total_pnl,
            )

        # 按目标指标排序
        results.sort(
            key=lambda r: getattr(r.metrics, self._sort_metric, 0.0),
            reverse=True,
        )
        return results

    def _generate_combinations(self) -> List[Dict[str, Any]]:
        """生成参数组合。"""
        if self._param_space.search_mode == "grid":
            return self._grid_search()
        elif self._param_space.search_mode == "random":
            return self._random_search()
        else:
            raise ValueError(f"Unknown search mode: {self._param_space.search_mode}")

    def _merged_params(self) -> Dict[str, List[Any]]:
        """合并策略参数和持仓参数为统一搜索空间。"""
        merged: Dict[str, List[Any]] = {}
        merged.update(self._param_space.strategy_params)
        merged.update(self._param_space.position_params)
        return merged

    def _grid_search(self) -> List[Dict[str, Any]]:
        """笛卡尔积展开所有参数组合。"""
        params = self._merged_params()
        if not params:
            return [{}]

        keys = list(params.keys())
        values = [params[k] for k in keys]

        combinations = []
        for combo in product(*values):
            combinations.append(dict(zip(keys, combo)))

        max_combos = self._param_space.max_combinations
        if len(combinations) > max_combos:
            logger.warning(
                "Grid search: %d combinations exceeds max %d, truncating",
                len(combinations),
                max_combos,
            )
            combinations = combinations[:max_combos]

        return combinations

    def _random_search(self) -> List[Dict[str, Any]]:
        """从参数空间随机采样。"""
        params = self._merged_params()
        if not params:
            return [{}]

        keys = list(params.keys())
        values = [params[k] for k in keys]
        max_combos = self._param_space.max_combinations

        # 计算全量组合数
        total_possible = 1
        for v in values:
            total_possible *= len(v)

        if total_possible <= max_combos:
            # 组合数不超过上限，直接返回全量
            return self._grid_search()

        # 随机采样
        seen: set[tuple] = set()
        combinations: List[Dict[str, Any]] = []
        max_attempts = max_combos * 10  # 防止无限循环

        for _ in range(max_attempts):
            if len(combinations) >= max_combos:
                break
            combo = tuple(random.choice(v) for v in values)
            if combo not in seen:
                seen.add(combo)
                combinations.append(dict(zip(keys, combo)))

        return combinations


def _extract_tf_param_overrides(
    module: SignalModule,
) -> tuple[Dict[str, Any], Dict[str, Dict[str, float]]]:
    """Extract effective global/per-TF strategy params from a SignalModule."""
    resolver = getattr(module, "_tf_param_resolver", None)
    if resolver is None or not hasattr(resolver, "dump"):
        return {}, {}

    global_params: Dict[str, Any] = {}
    per_tf_params: Dict[str, Dict[str, float]] = {}
    for compound_key, bucket in resolver.dump().items():
        if not isinstance(bucket, dict):
            continue
        for scope, value in bucket.items():
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                continue
            if scope == "__global__":
                global_params[compound_key] = numeric_value
            else:
                per_tf_params.setdefault(str(scope).upper(), {})[
                    compound_key
                ] = numeric_value

    return global_params, per_tf_params


def _extract_regime_affinity_overrides(
    module: SignalModule,
) -> Dict[str, Dict[str, float]]:
    """Extract the effective strategy regime affinities from a SignalModule."""
    overrides: Dict[str, Dict[str, float]] = {}
    for strategy_name, strategy in module._strategies.items():
        affinity_map = getattr(strategy, "regime_affinity", None)
        if not isinstance(affinity_map, dict):
            continue
        serialized: Dict[str, float] = {}
        for regime, value in affinity_map.items():
            regime_key = regime.value if isinstance(regime, RegimeType) else str(regime).lower()
            try:
                serialized[regime_key] = float(value)
            except (TypeError, ValueError):
                continue
        if serialized:
            overrides[strategy_name] = serialized
    return overrides


def _merge_nested_maps(
    base: Dict[str, Dict[str, float]],
    overrides: Optional[Dict[str, Dict[str, float]]],
) -> Dict[str, Dict[str, float]]:
    merged = {key: dict(value) for key, value in base.items()}
    if overrides:
        for key, value in overrides.items():
            merged.setdefault(key, {}).update(dict(value))
    return merged


def build_signal_module_with_overrides(
    base_module: SignalModule,
    param_overrides: Dict[str, Any],
    regime_affinity_overrides: Optional[Dict[str, Dict[str, float]]] = None,
    *,
    strategy_params_per_tf: Optional[Dict[str, Dict[str, float]]] = None,
) -> SignalModule:
    """构建带参数覆盖的独立 SignalModule 实例。

    复用 register_all_strategies() 的模式，但注入新的参数覆盖。

    Args:
        base_module: 基础 SignalModule 实例
        param_overrides: 策略参数覆盖（signal.ini [strategy_params] 格式）
        regime_affinity_overrides: Regime 亲和度覆盖（可选）
    """
    # 创建新的 SignalModule，复用相同的 indicator_source 和组件
    module = SignalModule(
        indicator_source=base_module.indicator_source,
        strategies=(),
        repository=None,  # 回测不写入 DB
        regime_detector=base_module._regime_detector,
        calibrator=base_module._calibrator,
        performance_tracker=base_module._performance_tracker,
        soft_regime_enabled=base_module._soft_regime_enabled,
        confidence_floor=base_module._confidence_floor,
    )

    # 重新注册所有单策略（从基础模块复制策略列表的类型来创建新实例）
    for name, strategy in base_module._strategies.items():
        if getattr(strategy, "metadata", {}).get("composite"):
            continue
        strategy_class = type(strategy)
        try:
            if isinstance(strategy, MultiTimeframeConfirmStrategy):
                new_instance = strategy_class(
                    state_reader=getattr(strategy, "_state_reader", None),
                    htf_cache=getattr(strategy, "_htf_cache", None),
                )
            else:
                new_instance = strategy_class()
            module.register_strategy(new_instance)
        except Exception:
            # 复合策略或需要参数的策略，直接跳过
            pass

    # 注册复合策略
    for composite in build_composite_strategies():
        try:
            module.register_strategy(composite)
        except Exception:
            pass

    base_strategy_params, base_strategy_params_per_tf = _extract_tf_param_overrides(
        base_module
    )
    merged_strategy_params = dict(base_strategy_params)
    merged_strategy_params.update(param_overrides)
    merged_strategy_params_per_tf = _merge_nested_maps(
        base_strategy_params_per_tf, strategy_params_per_tf
    )
    merged_regime_affinities = _merge_nested_maps(
        _extract_regime_affinity_overrides(base_module),
        regime_affinity_overrides,
    )

    # 保留基线 signal.ini / signal.local.ini 参数，再叠加本次优化覆盖。
    module.apply_param_overrides(
        merged_strategy_params,
        merged_regime_affinities or None,
        strategy_params_per_tf=merged_strategy_params_per_tf or None,
    )

    return module
