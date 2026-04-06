"""信号策略子模块

包含所有策略实现、复合策略、适配器和高时间框架缓存。
注意：registry 不在此处导入，因其依赖 service 模块，须直接从 strategies.registry 引入。

策略按类型分布在：
  base.py          — SignalStrategy 协议 + 工具函数
  trend.py         — 趋势跟踪策略（SMA、EMA Ribbon、MACD、Supertrend）
  mean_reversion.py— 均值回归策略（RSI、StochRSI）
  breakout.py      — 突破策略（Bollinger、Keltner-BB Squeeze、Donchian、假突破、MTF）
  session.py       — 时段偏置趋势策略
  price_action.py  — K线形态反转策略
"""

from .adapters import IndicatorSource, UnifiedIndicatorSourceAdapter
from .base import SignalStrategy, StrategyCategory, _resolve_indicator_value
from .legacy.breakout import (
    BollingerBreakoutStrategy,
    DonchianBreakoutStrategy,
    FakeBreakoutDetector,
    KeltnerBollingerSqueezeStrategy,
    MultiTimeframeConfirmStrategy,
    SqueezeReleaseFollow,
)
from .composite import CombineMode, CompositeSignalStrategy
from .htf_cache import HTFStateCache
from .legacy.mean_reversion import (
    RsiDivergenceStrategy,
    RsiReversionStrategy,
    StochRsiStrategy,
    VwapReversionStrategy,
)
from .legacy.multi_tf_entry import DualTFMomentum, HTFTrendPullback
from .legacy.price_action import OrderBlockEntryStrategy, PriceActionReversal
from .legacy.session import AsianRangeBreakout, SessionMomentumBias
from .structured import (
    StructuredBreakoutFollow,
    StructuredRangeReversion,
    StructuredSessionBreakout,
    StructuredSweepReversal,
    StructuredTrendContinuation,
    StructuredTrendlineTouch,
)
from .legacy.trend import (
    EmaRibbonStrategy,
    FibPullbackStrategy,
    MacdMomentumStrategy,
    SmaTrendStrategy,
    SupertrendStrategy,
)
from .legacy.trendline import TrendlineThreeTouchStrategy
from .legacy.volatility_structure import (
    AtrRegimeShift,
    BarMomentumSurge,
    RangeBoxBreakout,
    RangeMeanReversion,
    SwingStructureBreak,
)

__all__ = [
    "AsianRangeBreakout",
    "AtrRegimeShift",
    "BarMomentumSurge",
    "BollingerBreakoutStrategy",
    "CombineMode",
    "CompositeSignalStrategy",
    "DonchianBreakoutStrategy",
    "EmaRibbonStrategy",
    "FakeBreakoutDetector",
    "FibPullbackStrategy",
    "HTFTrendPullback",
    "HTFStateCache",
    "IndicatorSource",
    "KeltnerBollingerSqueezeStrategy",
    "MacdMomentumStrategy",
    "MultiTimeframeConfirmStrategy",
    "OrderBlockEntryStrategy",
    "PriceActionReversal",
    "RangeMeanReversion",
    "RangeBoxBreakout",
    "RsiDivergenceStrategy",
    "RsiReversionStrategy",
    "SessionMomentumBias",
    "SignalStrategy",
    "StrategyCategory",
    "SmaTrendStrategy",
    "StochRsiStrategy",
    "SqueezeReleaseFollow",
    "SupertrendStrategy",
    "StructuredBreakoutFollow",
    "StructuredRangeReversion",
    "StructuredSessionBreakout",
    "StructuredSweepReversal",
    "StructuredTrendContinuation",
    "StructuredTrendlineTouch",
    "SwingStructureBreak",
    "TrendlineThreeTouchStrategy",
    "UnifiedIndicatorSourceAdapter",
    "VwapReversionStrategy",
]
