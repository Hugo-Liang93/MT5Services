"""策略注册中心

将所有策略配置集中管理，与基础设施接线代码（deps.py）解耦。

使用方式
--------
deps.py 在构建 SignalModule 后分两个阶段调用：

  Phase 1（SignalRuntime 构建前）：
      register_composite_strategies(signal_module)

  Phase 2（HTFStateCache 构建后）：
      register_late_strategies(signal_module, htf_cache)

扩展
----
若要新增/修改复合策略，只需编辑本文件中的 `_COMPOSITE_STRATEGY_SPECS`，
无需触碰任何基础设施代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .composite import CompositeSignalStrategy, CombineMode
from .htf_cache import HTFStateCache
from .regime import RegimeType
from .service import SignalModule
from .strategies import (
    BollingerBreakoutStrategy,
    DonchianBreakoutStrategy,
    EmaRibbonStrategy,
    KeltnerBollingerSqueezeStrategy,
    MacdMomentumStrategy,
    MultiTimeframeConfirmStrategy,
    RsiReversionStrategy,
    SmaTrendStrategy,
    StochRsiStrategy,
    SupertrendStrategy,
)


@dataclass(frozen=True)
class CompositeSpec:
    """复合策略配置描述符（纯数据，不持有策略实例）。"""

    name: str
    sub_strategy_factories: Tuple[Any, ...]  # callable → strategy instance
    combine_mode: CombineMode
    regime_affinity: Dict[RegimeType, float]
    preferred_scopes: Tuple[str, ...]


# ── 内置复合策略配置表 ────────────────────────────────────────────────────────
# 每次 build() 时各 factory 都会创建独立实例，
# 避免与 SignalModule 中注册的单策略实例共享对象，
# 从而防止 VotingEngine 重复计票。
_COMPOSITE_STRATEGY_SPECS: List[CompositeSpec] = [
    # 趋势三重确认：Supertrend + MACD + SMA 多数同向时入场
    CompositeSpec(
        name="trend_triple_confirm",
        sub_strategy_factories=(SupertrendStrategy, MacdMomentumStrategy, SmaTrendStrategy),
        combine_mode="majority",
        regime_affinity={
            RegimeType.TRENDING:  1.00,  # 趋势行情核心策略，完全置信
            RegimeType.RANGING:   0.10,  # 震荡中趋势信号噪声高，几乎压制
            RegimeType.BREAKOUT:  0.55,  # 突破初期方向确认有用，中等权重
            RegimeType.UNCERTAIN: 0.40,  # 过渡区保守
        },
        preferred_scopes=("confirmed",),
    ),
    # 突破双重确认：Bollinger + Donchian + Keltner Squeeze 同时确认，过滤假突破
    CompositeSpec(
        name="breakout_double_confirm",
        sub_strategy_factories=(
            BollingerBreakoutStrategy,
            DonchianBreakoutStrategy,
            KeltnerBollingerSqueezeStrategy,
        ),
        combine_mode="all_agree",
        regime_affinity={
            RegimeType.TRENDING:  0.45,  # 趋势已确立后突破信号滞后，适中
            RegimeType.RANGING:   0.20,  # 震荡区间内假突破多，大幅压制
            RegimeType.BREAKOUT:  1.00,  # 突破行情完美场景
            RegimeType.UNCERTAIN: 0.55,  # 可能即将突破，给予中等权重
        },
        preferred_scopes=("confirmed",),
    ),
    # 震荡双重确认：RSI + StochRSI 同时超买/超卖的均值回归
    CompositeSpec(
        name="reversion_double_confirm",
        sub_strategy_factories=(RsiReversionStrategy, StochRsiStrategy),
        combine_mode="all_agree",
        regime_affinity={
            RegimeType.TRENDING:  0.15,  # 趋势中均值回归胜率极低
            RegimeType.RANGING:   1.00,  # 震荡行情核心策略
            RegimeType.BREAKOUT:  0.25,  # 突破行情中超买/超卖信号不可靠
            RegimeType.UNCERTAIN: 0.55,  # 中等保守
        },
        preferred_scopes=("confirmed", "intrabar"),
    ),
]


def build_composite_strategies() -> List[CompositeSignalStrategy]:
    """根据配置表构建复合策略实例列表。

    每次调用都会创建全新的子策略实例，保证各复合策略之间
    以及与 SignalModule 内单策略之间完全独立，
    防止同一实例在 VotingEngine 中被重复计票。
    """
    strategies = []
    for spec in _COMPOSITE_STRATEGY_SPECS:
        sub_instances = [factory() for factory in spec.sub_strategy_factories]
        strategies.append(
            CompositeSignalStrategy(
                name=spec.name,
                sub_strategies=sub_instances,
                combine_mode=spec.combine_mode,
                regime_affinity=spec.regime_affinity,
                preferred_scopes=spec.preferred_scopes,
            )
        )
    return strategies


def register_composite_strategies(module: SignalModule) -> None:
    """Phase 1：注册所有不依赖外部组件的复合策略。

    在 SignalRuntime 构建之前调用，使 SignalRuntime 的 targets
    列表中包含复合策略名称。
    """
    for strategy in build_composite_strategies():
        module.register_strategy(strategy)


def register_late_strategies(module: SignalModule, htf_cache: HTFStateCache) -> None:
    """Phase 2：注册依赖 HTFStateCache 的策略。

    HTFStateCache 在 SignalRuntime 和 TradeExecutor 构建完成后才可用，
    因此此函数须在 htf_cache.attach(signal_runtime) 之后调用。
    """
    module.register_strategy(MultiTimeframeConfirmStrategy(htf_cache=htf_cache))
