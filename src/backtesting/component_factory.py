"""回测组件构建工厂：CLI 和 API 共享。"""

from __future__ import annotations

import copy
import importlib
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from src.research.core.ports import ResearchDataDeps

logger = logging.getLogger(__name__)


def cleanup_components(components: Dict[str, Any]) -> None:
    """关闭 build_backtest_components 返的需托管资源（writer + pipeline）。

    §0cc P3 + §0dg P3：装配域 cleanup 责任的单一公开端口。原本定义在
    `src/backtesting/cli.py`，由 CLI / API / nightly / research deps 共享，
    导致 `component_factory.build_research_data_deps` 反向 `from cli import`，
    把"装配工厂"耦合到"命令行入口"——违反单向依赖。

    修复：把 helper 移到本模块（装配工厂）作为公开端口，cli.py 反向 import；
    依赖方向变为 cli → component_factory（正向）。

    每个步骤独立 try/except，让 cleanup 不会在 writer 失败时漏关 pipeline。
    """
    writer = components.get("writer")
    if writer is not None:
        close_writer = getattr(writer, "close", None)
        if callable(close_writer):
            try:
                close_writer()
            except Exception:
                logger.warning(
                    "backtesting cleanup: writer.close() failed", exc_info=True
                )
    pipeline = components.get("pipeline")
    if pipeline is not None:
        shutdown_pipeline = getattr(pipeline, "shutdown", None)
        if callable(shutdown_pipeline):
            try:
                shutdown_pipeline()
            except Exception:
                logger.warning(
                    "backtesting cleanup: pipeline.shutdown() failed", exc_info=True
                )


def _load_signal_config_snapshot():
    """加载 signal_config 的独立快照，不依赖 @lru_cache 全局单例。

    回测必须使用快照而非全局缓存，避免：
    1. 实盘 hot reload 后回测使用旧配置
    2. 多个并发回测共享同一个 config 对象
    """
    from src.config.signal import get_signal_config

    # 获取当前缓存版本的深拷贝
    cached = get_signal_config()
    return copy.deepcopy(cached)


def build_backtest_components(
    strategy_params: Optional[Dict[str, Any]] = None,
    regime_affinity_overrides: Optional[Dict[str, Dict[str, float]]] = None,
    strategy_params_per_tf: Optional[Dict[str, Dict[str, Any]]] = None,
    strategy_names: Optional[list[str]] = None,
) -> Dict[str, Any]:
    """构建回测所需的全部组件。

    CLI 和 API 统一入口，避免代码重复。

    Args:
        strategy_params: 策略参数覆盖（signal.ini [strategy_params] 格式）
        regime_affinity_overrides: Regime 亲和度覆盖
        strategy_params_per_tf: Per-TF 策略参数覆盖（signal.ini [strategy_params.<TF>] 格式）
        strategy_names: 仅注册这些策略；None = 默认全量策略目录

    Returns:
        包含 data_loader / signal_module / pipeline / regime_detector /
        writer / market_repo 的字典
    """
    from src.config.database import load_db_settings
    from src.config.indicator_config import get_global_config_manager
    from src.config.signal import get_signal_config
    from src.indicators.engine.pipeline import create_isolated_pipeline
    from src.persistence.db import TimescaleWriter
    from src.persistence.repositories.market_repo import MarketRepository
    from src.signals.evaluation.regime import MarketRegimeDetector
    from src.signals.service import SignalModule
    from src.signals.strategies.catalog import (
        build_default_strategy_set,
        clone_registered_strategies,
    )
    from src.signals.strategies.htf_cache import HTFStateCache

    from .data.loader import (
        CachingDataLoader,
        HistoricalDataLoader,
        get_shared_data_cache,
    )

    # DB 连接（独立连接池，不争抢生产连接）
    db_config = load_db_settings()
    writer = TimescaleWriter(settings=db_config, min_conn=1, max_conn=3)
    market_repo = MarketRepository(writer)
    # 使用 CachingDataLoader 透明包装：相同 (symbol, tf, 日期) 参数只查询 DB 一次
    raw_loader = HistoricalDataLoader(market_repo)
    data_loader = CachingDataLoader(raw_loader, get_shared_data_cache())

    # 指标管线（独立实例，不共享生产单例的缓存/线程池）
    config_manager = get_global_config_manager()
    indicator_config = config_manager.get_config()
    pipeline = create_isolated_pipeline(indicator_config.pipeline)

    # 注册指标函数
    for ind_cfg in indicator_config.indicators:
        parts = ind_cfg.func_path.rsplit(".", 1)
        mod = importlib.import_module(parts[0])
        func = getattr(mod, parts[1])
        pipeline.register_indicator(
            name=ind_cfg.name,
            func=func,
            params=ind_cfg.params,
            dependencies=ind_cfg.dependencies or None,
        )

    # 信号模块
    class _NullIndicatorSource:
        def get_indicator(
            self, symbol: str, timeframe: str, name: str
        ) -> Optional[Dict[str, Any]]:
            return None

        def get_all_indicators(
            self, symbol: str, timeframe: str
        ) -> Dict[str, Dict[str, Any]]:
            return {}

    regime_detector = MarketRegimeDetector()
    signal_config = _load_signal_config_snapshot()
    htf_cache = HTFStateCache(
        max_age_seconds=getattr(signal_config, "htf_cache_max_age_seconds", 14400),
    )

    # 构建 PerformanceTracker（与实盘共用同一逻辑）
    performance_tracker = None
    try:
        from src.signals.evaluation.performance import (
            PerformanceTrackerConfig,
            StrategyPerformanceTracker,
        )

        performance_tracker = StrategyPerformanceTracker(
            config=PerformanceTrackerConfig(enabled=True),
        )
    except Exception:
        logger.debug("PerformanceTracker not available for backtest", exc_info=True)

    signal_module = SignalModule(
        indicator_source=_NullIndicatorSource(),
        strategies=(
            clone_registered_strategies(strategy_names)
            if strategy_names
            else build_default_strategy_set()
        ),
        regime_detector=regime_detector,
        soft_regime_enabled=True,
        performance_tracker=performance_tracker,
    )

    # 回测基线默认对齐当前 signal.ini / signal.local.ini，再叠加显式请求覆盖。

    merged_strategy_params = dict(getattr(signal_config, "strategy_params", {}))
    if strategy_params:
        merged_strategy_params.update(strategy_params)

    merged_strategy_params_per_tf = {
        str(tf).upper(): dict(params)
        for tf, params in getattr(signal_config, "strategy_params_per_tf", {}).items()
    }
    if strategy_params_per_tf:
        for tf, params in strategy_params_per_tf.items():
            tf_key = str(tf).upper()
            merged_strategy_params_per_tf.setdefault(tf_key, {}).update(dict(params))

    merged_regime_affinities = {
        name: dict(values)
        for name, values in getattr(
            signal_config, "regime_affinity_overrides", {}
        ).items()
    }
    if regime_affinity_overrides:
        for strategy_name, affinity_map in regime_affinity_overrides.items():
            merged_regime_affinities.setdefault(strategy_name, {}).update(
                dict(affinity_map)
            )

    _apply_overrides(
        signal_module,
        merged_strategy_params,
        merged_regime_affinities or None,
        strategy_params_per_tf=merged_strategy_params_per_tf or None,
    )

    return {
        "data_loader": data_loader,
        "signal_module": signal_module,
        "pipeline": pipeline,
        "regime_detector": regime_detector,
        "performance_tracker": performance_tracker,
        "htf_cache": htf_cache,
        "writer": writer,
        "market_repo": market_repo,
    }


def _apply_overrides(
    module: Any,
    strategy_params: Dict[str, Any],
    regime_affinity_overrides: Optional[Dict[str, Dict[str, float]]] = None,
    *,
    strategy_params_per_tf: Optional[Dict[str, Dict[str, Any]]] = None,
) -> None:
    """应用策略参数和 Regime 亲和度覆盖（通过 SignalModule 公共 API）。"""
    module.apply_param_overrides(
        strategy_params,
        regime_affinity_overrides,
        strategy_params_per_tf=strategy_params_per_tf,
    )


def build_research_data_deps(
    strategy_params: Optional[Dict[str, Any]] = None,
    regime_affinity_overrides: Optional[Dict[str, Dict[str, float]]] = None,
    strategy_params_per_tf: Optional[Dict[str, Dict[str, Any]]] = None,
    strategy_names: Optional[list[str]] = None,
) -> "ResearchDataDeps":
    """从 backtesting 基础设施装配 ResearchDataDeps。

    这是 research 核心域对 backtesting 的**唯一正向适配入口**。
    装配层（CLI / API / nightly）调用此函数获取 deps，再注入 MiningRunner /
    build_data_matrix —— research 核心域本身不直接 import backtesting。

    Args:
        参数与 build_backtest_components 对齐，透传给底层。

    Returns:
        ResearchDataDeps（bar_loader / indicator_computer / regime_detector）。
    """
    from src.research.core.ports import ResearchDataDeps

    components = build_backtest_components(
        strategy_params=strategy_params,
        regime_affinity_overrides=regime_affinity_overrides,
        strategy_params_per_tf=strategy_params_per_tf,
        strategy_names=strategy_names,
    )
    # §0cc P2：把 writer + pipeline cleanup 责任显式回传给 consumer，
    # 避免 mining 入口（api/research_routes、ops/cli/mining_runner 等）
    # 拿不到合法释放端口导致连接池/线程池泄漏。
    # §0dg P3：cleanup_components 已迁到本模块（装配工厂），不再反向依赖 cli。
    return ResearchDataDeps(
        bar_loader=components["data_loader"],
        indicator_computer=components["pipeline"],
        regime_detector=components["regime_detector"],
        cleanup_fn=lambda: cleanup_components(components),
    )
