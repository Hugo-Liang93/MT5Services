"""回测组件构建工厂：CLI 和 API 共享。"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def build_backtest_components(
    strategy_params: Optional[Dict[str, Any]] = None,
    regime_affinity_overrides: Optional[Dict[str, Dict[str, float]]] = None,
) -> Dict[str, Any]:
    """构建回测所需的全部组件。

    CLI 和 API 统一入口，避免代码重复。

    Args:
        strategy_params: 策略参数覆盖（signal.ini [strategy_params] 格式）
        regime_affinity_overrides: Regime 亲和度覆盖

    Returns:
        包含 data_loader / signal_module / pipeline / regime_detector /
        voting_engine / writer / market_repo 的字典
    """
    from src.config.database import get_db_config
    from src.config.indicator_config import get_global_config_manager
    from src.indicators.engine.pipeline import get_global_pipeline
    from src.persistence.db import TimescaleWriter
    from src.persistence.repositories.market_repo import MarketRepository
    from src.signals.evaluation.regime import MarketRegimeDetector
    from src.signals.service import SignalModule

    from .data_loader import HistoricalDataLoader

    # DB 连接
    db_config = get_db_config()
    writer = TimescaleWriter(settings=db_config)
    market_repo = MarketRepository(writer)
    data_loader = HistoricalDataLoader(market_repo)

    # 指标管线
    config_manager = get_global_config_manager()
    indicator_config = config_manager.get_config()
    pipeline = get_global_pipeline(indicator_config.pipeline)

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

    # 注册复合策略
    from src.signals.strategies.registry import register_composite_strategies

    register_composite_strategies(signal_module)

    # 应用策略参数覆盖
    if strategy_params:
        _apply_overrides(signal_module, strategy_params, regime_affinity_overrides)
    elif regime_affinity_overrides:
        _apply_overrides(signal_module, {}, regime_affinity_overrides)

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
) -> None:
    """应用策略参数和 Regime 亲和度覆盖。"""
    from src.api.factories.signals import _apply_strategy_config_overrides

    class _FakeConfig:
        pass

    fake_config = _FakeConfig()
    fake_config.strategy_params = strategy_params  # type: ignore[attr-defined]
    fake_config.regime_affinity_overrides = regime_affinity_overrides or {}  # type: ignore[attr-defined]
    _apply_strategy_config_overrides(module, fake_config)


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
