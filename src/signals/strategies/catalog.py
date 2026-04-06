from __future__ import annotations

from collections import OrderedDict
from typing import Any, Iterable, Optional

from .base import SignalStrategy
from .composite import CompositeSignalStrategy
from .legacy.breakout import (
    BollingerBreakoutStrategy,
    DonchianBreakoutStrategy,
    FakeBreakoutDetector,
    KeltnerBollingerSqueezeStrategy,
    MultiTimeframeConfirmStrategy,
    SqueezeReleaseFollow,
)
from .legacy.m5_scalp import M5MomentumBurst, M5ScalpRSI
from .legacy.mean_reversion import (
    CciReversionStrategy,
    MacdDivergenceStrategy,
    RsiDivergenceStrategy,
    RsiReversionStrategy,
    StochRsiStrategy,
    VwapReversionStrategy,
    WilliamsRStrategy,
)
from .legacy.multi_tf_entry import DualTFMomentum, HTFTrendPullback
from .legacy.price_action import OrderBlockEntryStrategy, PriceActionReversal
from .legacy.session import AsianRangeBreakout, SessionMomentumBias
from .registry import build_composite_strategies
from .structured import (
    StructuredBreakoutFollow,
    StructuredRangeReversion,
    StructuredSessionBreakout,
    StructuredSweepReversal,
    StructuredTrendContinuation,
    StructuredTrendlineTouch,
)
from .legacy.trend import (
    AdxTrendFadeStrategy,
    EmaRibbonStrategy,
    FibPullbackStrategy,
    HmaCrossStrategy,
    MacdMomentumStrategy,
    RocMomentumStrategy,
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


def build_named_strategy_catalog(
    *,
    htf_cache: Optional[Any] = None,
    include_composites: bool = True,
) -> "OrderedDict[str, SignalStrategy]":
    strategies: "OrderedDict[str, SignalStrategy]" = OrderedDict()

    for strategy in (
        SmaTrendStrategy(),
        MacdMomentumStrategy(),
        SupertrendStrategy(),
        EmaRibbonStrategy(),
        HmaCrossStrategy(),
        RocMomentumStrategy(),
        FibPullbackStrategy(),
        SessionMomentumBias(),
        AsianRangeBreakout(),
        RsiReversionStrategy(),
        StochRsiStrategy(),
        WilliamsRStrategy(),
        CciReversionStrategy(),
        RsiDivergenceStrategy(),
        MacdDivergenceStrategy(),
        VwapReversionStrategy(),
        PriceActionReversal(),
        OrderBlockEntryStrategy(),
        BollingerBreakoutStrategy(),
        KeltnerBollingerSqueezeStrategy(),
        DonchianBreakoutStrategy(),
        FakeBreakoutDetector(),
        SqueezeReleaseFollow(),
        HTFTrendPullback(),
        HTFTrendPullback(name="htf_h4_pullback", htf="H4"),
        HTFTrendPullback(name="htf_m30_pullback", htf="M30"),
        DualTFMomentum(),
        DualTFMomentum(name="dual_h4_momentum", htf="H4"),
        M5ScalpRSI(),
        M5ScalpRSI(name="m5_scalp_rsi_h1", htf="H1"),
        M5MomentumBurst(),
        AdxTrendFadeStrategy(),
        TrendlineThreeTouchStrategy(),
        MultiTimeframeConfirmStrategy(htf_cache=htf_cache),
        RangeBoxBreakout(),
        BarMomentumSurge(),
        AtrRegimeShift(),
        SwingStructureBreak(),
        RangeMeanReversion(),
        StructuredTrendContinuation(),
        StructuredTrendContinuation(name="structured_trend_h4", htf="H4"),
        StructuredSweepReversal(),
        StructuredBreakoutFollow(),
        StructuredRangeReversion(),
        StructuredSessionBreakout(),
        StructuredTrendlineTouch(),
    ):
        strategies[strategy.name] = strategy

    if include_composites:
        for strategy in build_composite_strategies():
            strategies[strategy.name] = strategy

    return strategies


def build_default_strategy_set(
    *,
    htf_cache: Optional[Any] = None,
    include_composites: bool = True,
) -> list[SignalStrategy]:
    return list(
        build_named_strategy_catalog(
            htf_cache=htf_cache,
            include_composites=include_composites,
        ).values()
    )


def clone_registered_strategies(
    strategy_names: Iterable[str],
    *,
    htf_cache: Optional[Any] = None,
    include_composites: bool = True,
) -> list[SignalStrategy]:
    catalog = build_named_strategy_catalog(
        htf_cache=htf_cache,
        include_composites=include_composites,
    )
    cloned: list[SignalStrategy] = []
    missing: list[str] = []
    for name in strategy_names:
        strategy = catalog.get(str(name))
        if strategy is None:
            missing.append(str(name))
            continue
        cloned.append(strategy)
    if missing:
        raise ValueError(
            "Unregistered strategies requested from catalog: "
            + ", ".join(sorted(missing))
        )
    return cloned
