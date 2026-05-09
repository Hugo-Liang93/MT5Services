from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from src.clients.mt5_market import Tick
from src.market.tick_features.models import TickFeatureConfig, TickFeatureSnapshot


class TickFeatureCalculator:
    """Pure tick feature calculator shared by live and replay paths."""

    def __init__(self, config: Optional[TickFeatureConfig] = None) -> None:
        self._config = config or TickFeatureConfig()

    @property
    def config(self) -> TickFeatureConfig:
        return self._config

    def calculate(
        self,
        symbol: str,
        ticks: Iterable[Tick],
        now: datetime,
    ) -> TickFeatureSnapshot:
        now = self._as_utc(now)
        now_msc = int(now.timestamp() * 1000)
        ordered = sorted(
            [tick for tick in ticks if tick.time_msc is not None],
            key=lambda tick: int(tick.time_msc or 0),
        )
        if not ordered:
            return TickFeatureSnapshot(
                symbol=symbol,
                window_start_msc=now_msc - int(self._config.window_seconds * 1000),
                window_end_msc=now_msc,
                generated_at=now,
                tick_count=0,
                bid=None,
                ask=None,
                last=None,
                mid=None,
                spread_points=None,
                quote_age_ms=None,
                realized_range_points=None,
                price_change_points=None,
                buy_pressure=None,
                sell_pressure=None,
                status="stale",
                reasons=("no_ticks",),
            )

        latest = ordered[-1]
        latest_msc = int(latest.time_msc or now_msc)
        window_start_msc = latest_msc - int(self._config.window_seconds * 1000)
        window_ticks = [
            tick for tick in ordered if int(tick.time_msc or 0) >= window_start_msc
        ]
        latest = window_ticks[-1]
        prices = [
            price
            for price in (tick.price for tick in window_ticks)
            if price is not None
        ]
        first_price = prices[0] if prices else None
        latest_price = prices[-1] if prices else None
        bid = latest.bid
        ask = latest.ask
        mid = latest.mid
        spread_points = self._spread_points(symbol, bid, ask)
        quote_age_ms = now_msc - int(latest.time_msc or now_msc)
        realized_range_points = self._range_points(symbol, prices)
        price_change_points = self._price_change_points(
            symbol,
            first_price,
            latest_price,
        )
        buy_pressure, sell_pressure = self._direction_pressure(window_ticks)
        quote_side_missing = bid is None or ask is None
        status, reasons = self._status(
            symbol,
            len(window_ticks),
            spread_points,
            quote_age_ms,
            quote_side_missing,
        )

        return TickFeatureSnapshot(
            symbol=symbol,
            window_start_msc=window_start_msc,
            window_end_msc=int(latest.time_msc or latest_msc),
            generated_at=now,
            tick_count=len(window_ticks),
            bid=bid,
            ask=ask,
            last=latest.last,
            mid=mid,
            spread_points=spread_points,
            quote_age_ms=quote_age_ms,
            realized_range_points=realized_range_points,
            price_change_points=price_change_points,
            buy_pressure=buy_pressure,
            sell_pressure=sell_pressure,
            status=status,
            reasons=reasons,
        )

    def _spread_points(
        self,
        symbol: str,
        bid: Optional[float],
        ask: Optional[float],
    ) -> Optional[float]:
        if bid is None or ask is None:
            return None
        return round((ask - bid) / self._config.point_size_for(symbol), 10)

    def _range_points(self, symbol: str, prices: list[float]) -> Optional[float]:
        if not prices:
            return None
        return round(
            (max(prices) - min(prices)) / self._config.point_size_for(symbol),
            10,
        )

    def _price_change_points(
        self,
        symbol: str,
        first_price: Optional[float],
        latest_price: Optional[float],
    ) -> Optional[float]:
        if first_price is None or latest_price is None:
            return None
        return round(
            (latest_price - first_price) / self._config.point_size_for(symbol),
            10,
        )

    def _direction_pressure(
        self, ticks: list[Tick]
    ) -> tuple[Optional[float], Optional[float]]:
        previous: Optional[float] = None
        buys = 0
        sells = 0
        for tick in ticks:
            price = tick.price
            if price is None:
                continue
            if previous is not None:
                if price > previous:
                    buys += 1
                elif price < previous:
                    sells += 1
            previous = price
        moves = buys + sells
        if moves == 0:
            return 0.0, 0.0
        return buys / moves, sells / moves

    def _status(
        self,
        symbol: str,
        tick_count: int,
        spread_points: Optional[float],
        quote_age_ms: Optional[int],
        quote_side_missing: bool,
    ) -> tuple[str, tuple[str, ...]]:
        reasons: list[str] = []
        if tick_count < self._config.min_ticks_per_window:
            reasons.append("sparse_ticks")
        if quote_side_missing:
            reasons.append("quote_side_missing")
        if (
            spread_points is not None
            and spread_points > self._config.max_spread_points_for(symbol)
        ):
            reasons.append("spread_wide")
        if quote_age_ms is not None and quote_age_ms > self._config.max_quote_age_ms:
            reasons.append("quote_stale")
        if "quote_side_missing" in reasons or "spread_wide" in reasons:
            return "blocked", tuple(reasons)
        if "quote_stale" in reasons:
            return "stale", tuple(reasons)
        if reasons:
            return "sparse", tuple(reasons)
        return "healthy", ()

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
