"""回测组件构建工厂：CLI 和 API 共享。"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def build_backtest_components(
    strategy_params: Optional[Dict[str, Any]] = None,
    regime_affinity_overrides: Optional[Dict[str, Dict[str, float]]] = None,
    strategy_params_per_tf: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """构建回测所需的全部组件。

    CLI 和 API 统一入口，避免代码重复。

    Args:
        strategy_params: 策略参数覆盖（signal.ini [strategy_params] 格式）
        regime_affinity_overrides: Regime 亲和度覆盖
        strategy_params_per_tf: Per-TF 策略参数覆盖（signal.ini [strategy_params.<TF>] 格式）

    Returns:
        包含 data_loader / signal_module / pipeline / regime_detector /
        voting_engine / writer / market_repo 的字典
    """
    from src.config.database import get_db_config
    from src.config.indicator_config import get_global_config_manager
    from src.config.signal import get_signal_config
    from src.indicators.engine.pipeline import create_isolated_pipeline
    from src.persistence.db import TimescaleWriter
    from src.persistence.repositories.market_repo import MarketRepository
    from src.signals.evaluation.regime import MarketRegimeDetector
    from src.signals.service import SignalModule
    from src.signals.strategies.htf_cache import HTFStateCache
    from src.signals.strategies.registry import register_late_strategies

    from .data_loader import CachingDataLoader, HistoricalDataLoader, get_shared_data_cache

    # DB 连接（独立连接池，不争抢生产连接）
    db_config = get_db_config()
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
        if not ind_cfg.enabled:
            continue
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
    signal_module = SignalModule(
        indicator_source=_NullIndicatorSource(),
        regime_detector=regime_detector,
        soft_regime_enabled=True,
    )

    # 回测默认 SignalModule 已包含基础单策略和主要复合策略；
    # 这里补齐依赖 HTFStateCache 的 late strategies（如 multi_timeframe_confirm）。
    signal_config = get_signal_config()
    htf_cache = HTFStateCache(
        max_age_seconds=getattr(signal_config, "htf_cache_max_age_seconds", 14400),
    )
    register_late_strategies(signal_module, htf_cache)

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

    # 构建 Voting Engine（如果配置了 voting groups）
    voting_engine = _build_voting_engine(signal_module)

    return {
        "data_loader": data_loader,
        "signal_module": signal_module,
        "pipeline": pipeline,
        "regime_detector": regime_detector,
        "voting_engine": voting_engine,
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


def _build_voting_engine(signal_module: Any) -> Optional[Any]:
    """尝试从 signal.ini 配置构建 VotingEngine。"""
    try:
        from src.config.signal import get_signal_config
        from src.signals.orchestration.voting import StrategyVotingEngine

        signal_config = get_signal_config()
        policy = signal_config.policy if hasattr(signal_config, "policy") else None
        if policy and hasattr(policy, "voting_enabled") and policy.voting_enabled:
            return StrategyVotingEngine(
                consensus_threshold=getattr(policy, "consensus_threshold", 0.40),
            )
    except Exception:
        logger.debug("Voting engine not available", exc_info=True)
    return None
