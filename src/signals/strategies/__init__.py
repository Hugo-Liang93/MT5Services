"""信号策略子模块

包含所有策略实现、复合策略、适配器和高时间框架缓存。
注意：registry 不在此处导入，因其依赖 service 模块，须直接从 strategies.registry 引入。
"""

from .adapters import IndicatorSource, UnifiedIndicatorSourceAdapter
from .composite import CombineMode, CompositeSignalStrategy
from .htf_cache import HTFStateCache
from .library import (
    BollingerBreakoutStrategy,
    DonchianBreakoutStrategy,
    EmaRibbonStrategy,
    KeltnerBollingerSqueezeStrategy,
    MacdMomentumStrategy,
    MultiTimeframeConfirmStrategy,
    RsiReversionStrategy,
    SignalStrategy,
    SmaTrendStrategy,
    StochRsiStrategy,
    SupertrendStrategy,
)

__all__ = [
    "BollingerBreakoutStrategy",
    "CombineMode",
    "CompositeSignalStrategy",
    "DonchianBreakoutStrategy",
    "EmaRibbonStrategy",
    "HTFStateCache",
    "IndicatorSource",
    "KeltnerBollingerSqueezeStrategy",
    "MacdMomentumStrategy",
    "MultiTimeframeConfirmStrategy",
    "RsiReversionStrategy",
    "SignalStrategy",
    "SmaTrendStrategy",
    "StochRsiStrategy",
    "SupertrendStrategy",
    "UnifiedIndicatorSourceAdapter",
]
