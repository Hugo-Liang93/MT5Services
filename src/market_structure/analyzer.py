from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Iterable

from src.clients.mt5_market import OHLC
from src.signals.contracts import (
    SESSION_ASIA,
    SESSION_LONDON,
    SESSION_NEW_YORK,
    resolve_session_by_hour,
)

from .models import MarketStructureContext


@dataclass(frozen=True)
class MarketStructureConfig:
    enabled: bool = True
    lookback_bars: int = 400
    m1_lookback_bars: int = 120
    open_range_minutes: int = 60
    compression_window_bars: int = 6
    compression_reference_bars: int = 24


class MarketStructureAnalyzer:
    def __init__(self, market_service, config: MarketStructureConfig | None = None):
        self.market_service = market_service
        self.config = config or MarketStructureConfig()

    def analyze(
        self,
        symbol: str,
        timeframe: str,
        *,
        event_time: datetime | None = None,
        latest_close: float | None = None,
        lookback_bars_override: int | None = None,
    ) -> dict[str, object]:
        if not self.config.enabled:
            return {}

        lookback_bars = max(
            int(lookback_bars_override or self.config.lookback_bars),
            self.config.compression_window_bars
            + self.config.compression_reference_bars
            + 2,
        )
        bars = self.market_service.get_ohlc_closed(
            symbol,
            timeframe,
            limit=lookback_bars,
        )
        if not bars:
            return {}

        current_time = self._as_utc(event_time or bars[-1].time)
        bars = [bar for bar in bars if self._as_utc(bar.time) <= current_time]
        if not bars:
            return {}

        close_price = latest_close
        if close_price is None:
            close_price = float(bars[-1].close)

        current_day = current_time.date()
        previous_day = current_day - timedelta(days=1)
        previous_day_bars = self._bars_for_day(bars, previous_day)
        same_day_bars = self._bars_for_day(bars, current_day)
        asia_bars = self._bars_for_session(same_day_bars, SESSION_ASIA)
        london_open_bars = self._bars_for_open_range(
            same_day_bars,
            SESSION_LONDON,
            self.config.open_range_minutes,
        )
        new_york_open_bars = self._bars_for_open_range(
            same_day_bars,
            SESSION_NEW_YORK,
            self.config.open_range_minutes,
        )

        previous_day_high = self._max_high(previous_day_bars)
        previous_day_low = self._min_low(previous_day_bars)
        asia_range_high = self._max_high(asia_bars)
        asia_range_low = self._min_low(asia_bars)
        london_open_high = self._max_high(london_open_bars)
        london_open_low = self._min_low(london_open_bars)
        new_york_open_high = self._max_high(new_york_open_bars)
        new_york_open_low = self._min_low(new_york_open_bars)

        breakout_state, range_reference, breached_levels = self._resolve_breakout_state(
            close_price=close_price,
            previous_day_high=previous_day_high,
            previous_day_low=previous_day_low,
            asia_range_high=asia_range_high,
            asia_range_low=asia_range_low,
            london_open_high=london_open_high,
            london_open_low=london_open_low,
            new_york_open_high=new_york_open_high,
            new_york_open_low=new_york_open_low,
        )
        reclaim_state, swept_levels = self._resolve_reclaim_state(
            bars,
            close_price=close_price,
            previous_day_high=previous_day_high,
            previous_day_low=previous_day_low,
            asia_range_high=asia_range_high,
            asia_range_low=asia_range_low,
            london_open_high=london_open_high,
            london_open_low=london_open_low,
            new_york_open_high=new_york_open_high,
            new_york_open_low=new_york_open_low,
        )
        sweep_confirmation_state, confirmation_reference = (
            self._resolve_sweep_confirmation_state(
                bars,
                close_price=close_price,
                previous_day_high=previous_day_high,
                previous_day_low=previous_day_low,
                asia_range_high=asia_range_high,
                asia_range_low=asia_range_low,
                london_open_high=london_open_high,
                london_open_low=london_open_low,
                new_york_open_high=new_york_open_high,
                new_york_open_low=new_york_open_low,
            )
        )
        first_pullback_state, pullback_reference = self._resolve_first_pullback_state(
            bars,
            close_price=close_price,
            previous_day_high=previous_day_high,
            previous_day_low=previous_day_low,
            asia_range_high=asia_range_high,
            asia_range_low=asia_range_low,
            london_open_high=london_open_high,
            london_open_low=london_open_low,
            new_york_open_high=new_york_open_high,
            new_york_open_low=new_york_open_low,
        )
        compression_state, recent_range_avg, baseline_range_avg = (
            self._resolve_compression_state(bars)
        )
        structure_bias = self._resolve_structure_bias(
            breakout_state=breakout_state,
            reclaim_state=reclaim_state,
            sweep_confirmation_state=sweep_confirmation_state,
            first_pullback_state=first_pullback_state,
            compression_state=compression_state,
        )

        context = MarketStructureContext(
            symbol=symbol,
            timeframe=timeframe,
            analyzed_at=current_time,
            current_session=resolve_session_by_hour(current_time.hour),
            close_price=close_price,
            previous_day_high=previous_day_high,
            previous_day_low=previous_day_low,
            asia_range_high=asia_range_high,
            asia_range_low=asia_range_low,
            london_open_high=london_open_high,
            london_open_low=london_open_low,
            new_york_open_high=new_york_open_high,
            new_york_open_low=new_york_open_low,
            compression_state=compression_state,
            breakout_state=breakout_state,
            reclaim_state=reclaim_state,
            sweep_state=reclaim_state,
            sweep_confirmation_state=sweep_confirmation_state,
            first_pullback_state=first_pullback_state,
            structure_bias=structure_bias,
            range_reference=range_reference,
            confirmation_reference=confirmation_reference,
            pullback_reference=pullback_reference,
            breached_levels=tuple(breached_levels),
            swept_levels=tuple(swept_levels),
            recent_range_avg=recent_range_avg,
            baseline_range_avg=baseline_range_avg,
            bars_used=len(bars),
        )
        return context.to_dict()

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _bars_for_day(self, bars: Iterable[OHLC], day) -> list[OHLC]:
        return [bar for bar in bars if self._as_utc(bar.time).date() == day]

    def _bars_for_session(self, bars: Iterable[OHLC], session_name: str) -> list[OHLC]:
        return [
            bar
            for bar in bars
            if resolve_session_by_hour(self._as_utc(bar.time).hour) == session_name
        ]

    def _bars_for_open_range(
        self,
        bars: Iterable[OHLC],
        session_name: str,
        duration_minutes: int,
    ) -> list[OHLC]:
        session_hours = {
            SESSION_LONDON: 7,
            SESSION_NEW_YORK: 13,
        }
        start_hour = session_hours.get(session_name)
        if start_hour is None:
            return []
        result: list[OHLC] = []
        for bar in bars:
            ts = self._as_utc(bar.time)
            session_start = ts.replace(
                hour=start_hour,
                minute=0,
                second=0,
                microsecond=0,
            )
            session_end = session_start + timedelta(minutes=max(duration_minutes, 1))
            if session_start <= ts < session_end:
                result.append(bar)
        return result

    @staticmethod
    def _max_high(bars: Iterable[OHLC]) -> float | None:
        values = [float(bar.high) for bar in bars]
        return max(values) if values else None

    @staticmethod
    def _min_low(bars: Iterable[OHLC]) -> float | None:
        values = [float(bar.low) for bar in bars]
        return min(values) if values else None

    def _resolve_breakout_state(
        self,
        *,
        close_price: float | None,
        previous_day_high: float | None,
        previous_day_low: float | None,
        asia_range_high: float | None,
        asia_range_low: float | None,
        london_open_high: float | None,
        london_open_low: float | None,
        new_york_open_high: float | None,
        new_york_open_low: float | None,
    ) -> tuple[str, str | None, list[str]]:
        if close_price is None:
            return "none", None, []
        checks = (
            ("above_previous_day_high", previous_day_high, close_price > previous_day_high if previous_day_high is not None else False),
            ("below_previous_day_low", previous_day_low, close_price < previous_day_low if previous_day_low is not None else False),
            ("above_asia_high", asia_range_high, close_price > asia_range_high if asia_range_high is not None else False),
            ("below_asia_low", asia_range_low, close_price < asia_range_low if asia_range_low is not None else False),
            ("above_london_open_high", london_open_high, close_price > london_open_high if london_open_high is not None else False),
            ("below_london_open_low", london_open_low, close_price < london_open_low if london_open_low is not None else False),
            ("above_new_york_open_high", new_york_open_high, close_price > new_york_open_high if new_york_open_high is not None else False),
            ("below_new_york_open_low", new_york_open_low, close_price < new_york_open_low if new_york_open_low is not None else False),
        )
        breached_levels = [state for state, _level, matched in checks if matched]
        for state, level, matched in checks:
            if matched:
                if level is None:
                    return state, None, breached_levels
                if state.startswith("above_"):
                    return state, state.removeprefix("above_"), breached_levels
                if state.startswith("below_"):
                    return state, state.removeprefix("below_"), breached_levels
                return state, state, breached_levels
        return "none", None, breached_levels

    def _resolve_reclaim_state(
        self,
        bars: list[OHLC],
        *,
        close_price: float | None,
        previous_day_high: float | None,
        previous_day_low: float | None,
        asia_range_high: float | None,
        asia_range_low: float | None,
        london_open_high: float | None,
        london_open_low: float | None,
        new_york_open_high: float | None,
        new_york_open_low: float | None,
    ) -> tuple[str, list[str]]:
        if close_price is None or not bars:
            return "none", []
        last_bar = bars[-1]
        checks = (
            (
                "bearish_reclaim_previous_day_high",
                previous_day_high,
                float(last_bar.high) > previous_day_high and close_price < previous_day_high
                if previous_day_high is not None
                else False,
            ),
            (
                "bullish_reclaim_previous_day_low",
                previous_day_low,
                float(last_bar.low) < previous_day_low and close_price > previous_day_low
                if previous_day_low is not None
                else False,
            ),
            (
                "bearish_reclaim_asia_high",
                asia_range_high,
                float(last_bar.high) > asia_range_high and close_price < asia_range_high
                if asia_range_high is not None
                else False,
            ),
            (
                "bullish_reclaim_asia_low",
                asia_range_low,
                float(last_bar.low) < asia_range_low and close_price > asia_range_low
                if asia_range_low is not None
                else False,
            ),
            (
                "bearish_reclaim_london_open_high",
                london_open_high,
                float(last_bar.high) > london_open_high and close_price < london_open_high
                if london_open_high is not None
                else False,
            ),
            (
                "bullish_reclaim_london_open_low",
                london_open_low,
                float(last_bar.low) < london_open_low and close_price > london_open_low
                if london_open_low is not None
                else False,
            ),
            (
                "bearish_reclaim_new_york_open_high",
                new_york_open_high,
                float(last_bar.high) > new_york_open_high and close_price < new_york_open_high
                if new_york_open_high is not None
                else False,
            ),
            (
                "bullish_reclaim_new_york_open_low",
                new_york_open_low,
                float(last_bar.low) < new_york_open_low and close_price > new_york_open_low
                if new_york_open_low is not None
                else False,
            ),
        )
        swept_levels = [state for state, _level, matched in checks if matched]
        for state, _level, matched in checks:
            if matched:
                return state, swept_levels
        return "none", swept_levels

    def _resolve_compression_state(
        self,
        bars: list[OHLC],
    ) -> tuple[str, float | None, float | None]:
        required = self.config.compression_window_bars + self.config.compression_reference_bars
        if len(bars) < required:
            return "unknown", None, None
        ranges = [float(bar.high) - float(bar.low) for bar in bars]
        recent_values = ranges[-self.config.compression_window_bars :]
        baseline_values = ranges[
            -required : -self.config.compression_window_bars
        ]
        recent_avg = mean(recent_values)
        baseline_avg = mean(baseline_values) if baseline_values else None
        if baseline_avg is None or baseline_avg <= 0:
            return "unknown", recent_avg, baseline_avg
        if recent_avg <= baseline_avg * 0.7:
            return "contracted", recent_avg, baseline_avg
        if recent_avg >= baseline_avg * 1.3:
            return "expanded", recent_avg, baseline_avg
        return "normal", recent_avg, baseline_avg

    def _resolve_first_pullback_state(
        self,
        bars: list[OHLC],
        *,
        close_price: float | None,
        previous_day_high: float | None,
        previous_day_low: float | None,
        asia_range_high: float | None,
        asia_range_low: float | None,
        london_open_high: float | None,
        london_open_low: float | None,
        new_york_open_high: float | None,
        new_york_open_low: float | None,
    ) -> tuple[str, str | None]:
        if close_price is None or len(bars) < 2:
            return "none", None
        previous_close = float(bars[-2].close)
        last_bar = bars[-1]

        bullish_checks = (
            ("previous_day_high", previous_day_high),
            ("asia_high", asia_range_high),
            ("london_open_high", london_open_high),
            ("new_york_open_high", new_york_open_high),
        )
        for reference, level in bullish_checks:
            if level is None:
                continue
            if previous_close > level and float(last_bar.low) <= level < close_price:
                return f"bullish_first_pullback_{reference}", reference

        bearish_checks = (
            ("previous_day_low", previous_day_low),
            ("asia_low", asia_range_low),
            ("london_open_low", london_open_low),
            ("new_york_open_low", new_york_open_low),
        )
        for reference, level in bearish_checks:
            if level is None:
                continue
            if previous_close < level and float(last_bar.high) >= level > close_price:
                return f"bearish_first_pullback_{reference}", reference

        return "none", None

    def _resolve_sweep_confirmation_state(
        self,
        bars: list[OHLC],
        *,
        close_price: float | None,
        previous_day_high: float | None,
        previous_day_low: float | None,
        asia_range_high: float | None,
        asia_range_low: float | None,
        london_open_high: float | None,
        london_open_low: float | None,
        new_york_open_high: float | None,
        new_york_open_low: float | None,
    ) -> tuple[str, str | None]:
        if close_price is None or len(bars) < 2:
            return "none", None
        previous_bar = bars[-1]
        anchor_close = float(previous_bar.close)

        bearish_checks = (
            ("previous_day_high", previous_day_high),
            ("asia_high", asia_range_high),
            ("london_open_high", london_open_high),
            ("new_york_open_high", new_york_open_high),
        )
        for reference, level in bearish_checks:
            if level is None:
                continue
            if (
                float(previous_bar.high) > level
                and anchor_close < level
                and close_price < anchor_close
            ):
                return f"bearish_sweep_confirmed_{reference}", reference

        bullish_checks = (
            ("previous_day_low", previous_day_low),
            ("asia_low", asia_range_low),
            ("london_open_low", london_open_low),
            ("new_york_open_low", new_york_open_low),
        )
        for reference, level in bullish_checks:
            if level is None:
                continue
            if (
                float(previous_bar.low) < level
                and anchor_close > level
                and close_price > anchor_close
            ):
                return f"bullish_sweep_confirmed_{reference}", reference

        return "none", None

    @staticmethod
    def _resolve_structure_bias(
        *,
        breakout_state: str,
        reclaim_state: str,
        sweep_confirmation_state: str,
        first_pullback_state: str,
        compression_state: str,
    ) -> str:
        if sweep_confirmation_state.startswith("bullish_"):
            return "bullish_sweep_confirmed"
        if sweep_confirmation_state.startswith("bearish_"):
            return "bearish_sweep_confirmed"
        if reclaim_state.startswith("bullish_"):
            return "bullish_reclaim"
        if reclaim_state.startswith("bearish_"):
            return "bearish_reclaim"
        if first_pullback_state.startswith("bullish_"):
            return "bullish_pullback"
        if first_pullback_state.startswith("bearish_"):
            return "bearish_pullback"
        if breakout_state.startswith("above_"):
            return "bullish_breakout"
        if breakout_state.startswith("below_"):
            return "bearish_breakout"
        if compression_state == "contracted":
            return "compression"
        if compression_state == "expanded":
            return "expansion"
        return "neutral"
