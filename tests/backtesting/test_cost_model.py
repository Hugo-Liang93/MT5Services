"""动态成本模型（dynamic spread + overnight swap）测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.backtesting.engine.portfolio import CostModel


def _t(hour: int, weekday_offset: int = 0) -> datetime:
    # 2026-01-01 is a Thursday (weekday=3). weekday_offset shifts day.
    base_day = 1 + weekday_offset
    return datetime(2026, 1, base_day, hour, 0, tzinfo=timezone.utc)


class TestSessionMult:
    def test_asia_session(self) -> None:
        cm = CostModel(
            dynamic_spread_enabled=True,
            spread_base_points=15.0,
            spread_asia_mult=1.0,
            spread_london_mult=1.2,
            spread_ny_mult=1.3,
        )
        # 03 UTC = 亚盘
        assert cm.session_mult(_t(3)) == 1.0

    def test_london_session(self) -> None:
        cm = CostModel(dynamic_spread_enabled=True, spread_london_mult=1.2)
        assert cm.session_mult(_t(9)) == 1.2

    def test_ny_session(self) -> None:
        cm = CostModel(dynamic_spread_enabled=True, spread_ny_mult=1.3)
        # 14 UTC = 欧美重叠，归为 NY
        assert cm.session_mult(_t(14)) == 1.3


class TestEffectiveSpread:
    def test_disabled_returns_zero(self) -> None:
        cm = CostModel(dynamic_spread_enabled=False)
        assert cm.effective_spread_points(_t(10), atr_ratio=3.0) == 0.0

    def test_baseline_no_volatility(self) -> None:
        cm = CostModel(
            dynamic_spread_enabled=True,
            spread_base_points=15.0,
            spread_london_mult=1.2,
            spread_volatility_threshold=2.0,
            spread_volatility_mult=2.0,
        )
        # 欧盘 + 波动正常
        assert cm.effective_spread_points(_t(9), atr_ratio=1.0) == pytest.approx(18.0)

    def test_high_volatility_doubles(self) -> None:
        cm = CostModel(
            dynamic_spread_enabled=True,
            spread_base_points=15.0,
            spread_london_mult=1.2,
            spread_volatility_threshold=1.8,
            spread_volatility_mult=2.0,
        )
        # 欧盘 + 高波动（ATR ratio >= threshold）
        assert cm.effective_spread_points(_t(9), atr_ratio=2.0) == pytest.approx(36.0)


class TestSwapCharge:
    def test_disabled_returns_zero(self) -> None:
        cm = CostModel(swap_enabled=False)
        assert cm.swap_charge("buy", 1.0, _t(21)) == 0.0

    def test_long_pays_negative(self) -> None:
        cm = CostModel(
            swap_enabled=True,
            swap_long_per_lot=-0.3,
            swap_short_per_lot=0.15,
            swap_wednesday_triple=False,
        )
        # Thursday (weekday=3), not Wednesday
        assert cm.swap_charge("buy", 2.0, _t(21)) == pytest.approx(-0.6)

    def test_short_receives_positive(self) -> None:
        cm = CostModel(
            swap_enabled=True,
            swap_long_per_lot=-0.3,
            swap_short_per_lot=0.15,
            swap_wednesday_triple=False,
        )
        assert cm.swap_charge("sell", 2.0, _t(21)) == pytest.approx(0.3)

    def test_wednesday_triple(self) -> None:
        cm = CostModel(
            swap_enabled=True,
            swap_long_per_lot=-0.3,
            swap_wednesday_triple=True,
        )
        # 2026-01-07 is Wednesday (weekday=2)
        wed = datetime(2026, 1, 7, 21, 0, tzinfo=timezone.utc)
        assert cm.swap_charge("buy", 1.0, wed) == pytest.approx(-0.9)

    def test_wednesday_not_tripled_when_flag_off(self) -> None:
        cm = CostModel(
            swap_enabled=True,
            swap_long_per_lot=-0.3,
            swap_wednesday_triple=False,
        )
        wed = datetime(2026, 1, 7, 21, 0, tzinfo=timezone.utc)
        assert cm.swap_charge("buy", 1.0, wed) == pytest.approx(-0.3)
