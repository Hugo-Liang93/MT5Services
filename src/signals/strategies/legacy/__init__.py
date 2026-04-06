"""Legacy 策略子包

包含早期"指标翻译器"策略，已不再活跃使用但保留代码资产。
所有策略类在此统一重导出，保持向后兼容。
"""

from .breakout import (
    BollingerBreakoutStrategy,
    DonchianBreakoutStrategy,
    FakeBreakoutDetector,
    KeltnerBollingerSqueezeStrategy,
    MultiTimeframeConfirmStrategy,
    SqueezeReleaseFollow,
)
from .m5_scalp import M5MomentumBurst, M5ScalpRSI
from .mean_reversion import (
    CciReversionStrategy,
    MacdDivergenceStrategy,
    RsiDivergenceStrategy,
    RsiReversionStrategy,
    StochRsiStrategy,
    VwapReversionStrategy,
    WilliamsRStrategy,
    configure_delta_params,
)
from .multi_tf_entry import DualTFMomentum, HTFTrendPullback
from .price_action import OrderBlockEntryStrategy, PriceActionReversal
from .session import AsianRangeBreakout, SessionMomentumBias
from .trend import (
    AdxTrendFadeStrategy,
    EmaRibbonStrategy,
    FibPullbackStrategy,
    HmaCrossStrategy,
    MacdMomentumStrategy,
    RocMomentumStrategy,
    SmaTrendStrategy,
    SupertrendStrategy,
)
from .trendline import TrendlineThreeTouchStrategy
from .volatility_structure import (
    AtrRegimeShift,
    BarMomentumSurge,
    RangeBoxBreakout,
    RangeMeanReversion,
    SwingStructureBreak,
)

__all__ = [
    "AdxTrendFadeStrategy",
    "AsianRangeBreakout",
    "AtrRegimeShift",
    "BarMomentumSurge",
    "BollingerBreakoutStrategy",
    "CciReversionStrategy",
    "DonchianBreakoutStrategy",
    "DualTFMomentum",
    "EmaRibbonStrategy",
    "FakeBreakoutDetector",
    "FibPullbackStrategy",
    "HTFTrendPullback",
    "HmaCrossStrategy",
    "KeltnerBollingerSqueezeStrategy",
    "M5MomentumBurst",
    "M5ScalpRSI",
    "MacdDivergenceStrategy",
    "MacdMomentumStrategy",
    "MultiTimeframeConfirmStrategy",
    "OrderBlockEntryStrategy",
    "PriceActionReversal",
    "RangeBoxBreakout",
    "RangeMeanReversion",
    "RocMomentumStrategy",
    "RsiDivergenceStrategy",
    "RsiReversionStrategy",
    "SessionMomentumBias",
    "SmaTrendStrategy",
    "SqueezeReleaseFollow",
    "StochRsiStrategy",
    "SupertrendStrategy",
    "SwingStructureBreak",
    "TrendlineThreeTouchStrategy",
    "VwapReversionStrategy",
    "WilliamsRStrategy",
    "configure_delta_params",
]
