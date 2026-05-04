from __future__ import annotations

import pytest

from src.signals.metadata_keys import MetadataKey as MK
from src.signals.models import SignalContext
from src.signals.strategies.structured.base import ExitMode
from src.signals.strategies.structured.micro_momentum import StructuredMicroMomentum


def _context(
    *,
    timeframe: str = "M1",
    structure_type: float = 1.0,
    trend_bars: float = 3.0,
    close_position: float = 0.78,
    plus_di: float = 30.0,
    minus_di: float = 18.0,
    volume_ratio: float = 1.35,
) -> SignalContext:
    close = 100.80 if close_position >= 0.5 else 99.20
    return SignalContext(
        symbol="XAUUSD",
        timeframe=timeframe,
        strategy="structured_micro_momentum",
        indicators={
            "bar_stats20": {
                "body_ratio": 1.35,
                "close_position": close_position,
            },
            "price_struct20": {
                "structure_type": structure_type,
                "trend_bars": trend_bars,
            },
            "atr14": {"atr": 1.25},
            "boll20": {
                "bb_upper": 101.0,
                "bb_lower": 99.0,
                "close": close,
            },
            "keltner20": {
                "kc_upper": 101.2,
                "kc_lower": 98.8,
            },
            "adx14": {
                "adx": 23.0,
                "plus_di": plus_di,
                "minus_di": minus_di,
            },
            "volume_ratio20": {"volume_ratio": volume_ratio},
        },
    )


def test_micro_momentum_emits_buy_from_confirmed_m1_bar() -> None:
    strategy = StructuredMicroMomentum()

    decision = strategy.evaluate(_context(timeframe="M1"))

    assert decision.strategy == "structured_micro_momentum"
    assert decision.direction == "buy"
    assert decision.confidence >= 0.70
    assert decision.metadata[MK.ENTRY_INTENT]["timeframe"] == "M1"
    assert decision.metadata[MK.ENTRY_INTENT]["direction"] == "buy"
    assert decision.metadata["exit_spec"]["mode"] == ExitMode.BARRIER.value
    assert decision.metadata["exit_spec"]["sl_atr"] == pytest.approx(0.90)
    assert decision.metadata["exit_spec"]["tp_atr"] == pytest.approx(1.35)
    assert decision.metadata["exit_spec"]["time_bars"] == 8
    assert "volume_ratio20" in decision.used_indicators


def test_micro_momentum_emits_sell_from_confirmed_m5_bar() -> None:
    strategy = StructuredMicroMomentum()

    decision = strategy.evaluate(
        _context(
            timeframe="M5",
            structure_type=-1.0,
            trend_bars=-3.0,
            close_position=0.22,
            plus_di=18.0,
            minus_di=31.0,
        )
    )

    assert decision.direction == "sell"
    assert decision.metadata[MK.ENTRY_INTENT]["timeframe"] == "M5"
    assert decision.metadata["exit_spec"]["sl_atr"] == pytest.approx(1.10)
    assert decision.metadata["exit_spec"]["tp_atr"] == pytest.approx(1.65)
    assert decision.metadata["exit_spec"]["time_bars"] == 6


def test_micro_momentum_blocks_low_volume_bar() -> None:
    strategy = StructuredMicroMomentum()

    decision = strategy.evaluate(_context(volume_ratio=0.90))

    assert decision.direction == "hold"
    assert decision.reason == "volume_low:0.90<1.15"

