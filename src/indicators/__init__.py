"""Indicator helpers aggregation and worker entrypoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types import IndicatorTask

if TYPE_CHECKING:
    from .mean import ema, sma, wma
    from .momentum import cci, macd, roc, rsi
    from .volatility import atr, bollinger, donchian, keltner
    from .volume import obv, vwap
    from .worker import IndicatorSnapshot, IndicatorWorker

__all__ = [
    "IndicatorTask",
    "IndicatorSnapshot",
    "IndicatorWorker",
    "sma",
    "ema",
    "wma",
    "rsi",
    "macd",
    "roc",
    "cci",
    "atr",
    "bollinger",
    "keltner",
    "donchian",
    "obv",
    "vwap",
]


def __getattr__(name: str):
    # Lazy import breaks config<->indicators import cycle while keeping public API.
    if name in {"IndicatorSnapshot", "IndicatorWorker"}:
        from .worker import IndicatorSnapshot, IndicatorWorker

        return {"IndicatorSnapshot": IndicatorSnapshot, "IndicatorWorker": IndicatorWorker}[name]
    if name in {"sma", "ema", "wma"}:
        from . import mean

        return {"sma": mean.sma, "ema": mean.ema, "wma": mean.wma}[name]
    if name in {"rsi", "macd", "roc", "cci"}:
        from . import momentum

        return {"rsi": momentum.rsi, "macd": momentum.macd, "roc": momentum.roc, "cci": momentum.cci}[name]
    if name in {"atr", "bollinger", "keltner", "donchian"}:
        from . import volatility

        return {
            "atr": volatility.atr,
            "bollinger": volatility.bollinger,
            "keltner": volatility.keltner,
            "donchian": volatility.donchian,
        }[name]
    if name in {"obv", "vwap"}:
        from . import volume

        return {"obv": volume.obv, "vwap": volume.vwap}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
