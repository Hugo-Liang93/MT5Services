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

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from .composite import CompositeSignalStrategy, CombineMode
from .htf_cache import HTFStateCache
from ..evaluation.regime import RegimeType
from ..service import SignalModule
from .breakout import (
    BollingerBreakoutStrategy,
    DonchianBreakoutStrategy,
    KeltnerBollingerSqueezeStrategy,
    MultiTimeframeConfirmStrategy,
)
from .mean_reversion import RsiReversionStrategy, StochRsiStrategy
from .trend import EmaRibbonStrategy, MacdMomentumStrategy, SmaTrendStrategy, SupertrendStrategy


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


def _load_specs_from_json(path: str) -> Optional[List[CompositeSpec]]:
    """V-3: 从 composites.json 加载复合策略配置，返回 CompositeSpec 列表。

    JSON 中使用策略类名字符串（如 "SupertrendStrategy"），此函数将其解析为工厂函数。
    解析失败时返回 None，调用方应回退到硬编码默认值。
    """
    _CLASS_MAP: Dict[str, Any] = {
        "BollingerBreakoutStrategy": BollingerBreakoutStrategy,
        "DonchianBreakoutStrategy": DonchianBreakoutStrategy,
        "EmaRibbonStrategy": EmaRibbonStrategy,
        "KeltnerBollingerSqueezeStrategy": KeltnerBollingerSqueezeStrategy,
        "MacdMomentumStrategy": MacdMomentumStrategy,
        "RsiReversionStrategy": RsiReversionStrategy,
        "SmaTrendStrategy": SmaTrendStrategy,
        "StochRsiStrategy": StochRsiStrategy,
        "SupertrendStrategy": SupertrendStrategy,
    }
    _REGIME_MAP: Dict[str, "RegimeType"] = {
        "TRENDING": RegimeType.TRENDING,
        "RANGING": RegimeType.RANGING,
        "BREAKOUT": RegimeType.BREAKOUT,
        "UNCERTAIN": RegimeType.UNCERTAIN,
    }
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fp:
            raw_list = json.load(fp)
        specs: List[CompositeSpec] = []
        for item in raw_list:
            factories = tuple(
                _CLASS_MAP[cls_name]
                for cls_name in item["sub_strategies"]
                if cls_name in _CLASS_MAP
            )
            if not factories:
                logger.warning(
                    "strategy_registry: skip composite '%s' — no valid sub_strategies",
                    item.get("name"),
                )
                continue
            affinity = {
                _REGIME_MAP[k]: float(v)
                for k, v in item.get("regime_affinity", {}).items()
                if k in _REGIME_MAP
            }
            specs.append(
                CompositeSpec(
                    name=item["name"],
                    sub_strategy_factories=factories,
                    combine_mode=item.get("combine_mode", "majority"),
                    regime_affinity=affinity,
                    preferred_scopes=tuple(item.get("preferred_scopes", ["confirmed"])),
                )
            )
        logger.info(
            "strategy_registry: loaded %d composite specs from %s", len(specs), path
        )
        return specs
    except Exception:
        logger.warning(
            "strategy_registry: failed to load composites from %s, using defaults",
            path,
            exc_info=True,
        )
        return None


def build_composite_strategies(
    *, config_path: Optional[str] = None
) -> List[CompositeSignalStrategy]:
    """根据配置表构建复合策略实例列表。

    V-3: 优先从 ``config_path``（默认 config/composites.json）加载策略规格，
    加载失败时回退到硬编码的 ``_COMPOSITE_STRATEGY_SPECS``。

    每次调用都会创建全新的子策略实例，保证各复合策略之间
    以及与 SignalModule 内单策略之间完全独立，
    防止同一实例在 VotingEngine 中被重复计票。
    """
    if config_path is None:
        # 自动定位：从本文件向上两级找到项目根目录
        _here = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(_here, "..", "..", "config", "composites.json")

    specs = _load_specs_from_json(config_path) or _COMPOSITE_STRATEGY_SPECS
    strategies = []
    for spec in specs:
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
    """注册所有不依赖外部组件的复合策略。

    必须在 SignalRuntime 构建之前调用，以确保 runtime_targets 包含这些策略名。
    """
    for strategy in build_composite_strategies():
        module.register_strategy(strategy)


def register_late_strategies(module: SignalModule, htf_cache: HTFStateCache) -> None:
    """注册依赖 HTFStateCache 的策略（MultiTimeframeConfirmStrategy 等）。

    HTFStateCache 构建后即可调用，无需等待 SignalRuntime 完成初始化。
    同样必须在 SignalRuntime 构建之前调用（与 register_composite_strategies 相同要求），
    确保 runtime_targets 包含 MTF 策略名称，否则 SignalRuntime._target_index 不会收录该策略，
    导致快照事件无法路由给 MultiTimeframeConfirmStrategy。
    """
    module.register_strategy(MultiTimeframeConfirmStrategy(htf_cache=htf_cache))


def register_all_strategies(module: SignalModule, htf_cache: HTFStateCache) -> None:
    """一次性注册所有策略（复合策略 + HTF 确认策略）。

    调用时机
    --------
    必须在 ``SignalRuntime.__init__`` 之前完成，即在 runtime_targets 列表构建前调用。
    ``HTFStateCache`` 只需在本函数之前创建即可，无需等待 ``SignalRuntime``。

    典型用法（deps.py）::

        _c.htf_cache = HTFStateCache()
        register_all_strategies(_c.signal_module, _c.htf_cache)
        runtime_targets = [...]           # 现在包含所有策略
        _c.signal_runtime = SignalRuntime(targets=runtime_targets, ...)
        _c.htf_cache.attach(_c.signal_runtime)   # 注册为信号监听器
    """
    register_composite_strategies(module)
    register_late_strategies(module, htf_cache)
