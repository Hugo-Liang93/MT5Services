"""信号策略子模块

包含所有策略实现、复合策略、适配器和高时间框架缓存。
注意：registry 不在此处导入，因其依赖 service 模块，须直接从 strategies.registry 引入。

策略按类型分布在：
  base.py          — SignalStrategy 协议 + 工具函数
  trend.py         — 趋势跟踪策略（SMA、EMA Ribbon、MACD、Supertrend）
  mean_reversion.py— 均值回归策略（RSI、StochRSI）
  breakout.py      — 突破策略（Bollinger、Keltner-BB Squeeze、Donchian、MTF）
"""

from .adapters import IndicatorSource, UnifiedIndicatorSourceAdapter
from .base import SignalStrategy, _resolve_indicator_value
from .breakout import (
    BollingerBreakoutStrategy,
    DonchianBreakoutStrategy,
    KeltnerBollingerSqueezeStrategy,
    MultiTimeframeConfirmStrategy,
)
from .composite import CombineMode, CompositeSignalStrategy
from .htf_cache import HTFStateCache
from .mean_reversion import RsiReversionStrategy, StochRsiStrategy
from .trend import EmaRibbonStrategy, MacdMomentumStrategy, SmaTrendStrategy, SupertrendStrategy

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
