"""PendingEntryManager 单元测试。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

from src.signals.models import SignalEvent
from src.trading.pending_entry import (
    PendingEntry,
    PendingEntryConfig,
    PendingEntryManager,
    compute_entry_zone,
    compute_timeout,
    _CATEGORY_ZONE_MODE,
)
from src.trading.sizing import TradeParameters


# ── Fixtures ──────────────────────────────────────────────────────────────


def _make_signal_event(
    *,
    symbol: str = "XAUUSD",
    timeframe: str = "M5",
    strategy: str = "supertrend",
    action: str = "buy",
    confidence: float = 0.8,
    signal_state: str = "confirmed_buy",
    scope: str = "confirmed",
    signal_id: str = "sig-001",
    category: str = "trend",
    indicators: Optional[Dict] = None,
) -> SignalEvent:
    return SignalEvent(
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy,
        action=action,
        confidence=confidence,
        signal_state=signal_state,
        scope=scope,
        indicators=indicators or {"atr14": {"atr": 3.5}},
        metadata={"category": category, "close_price": 2650.0},
        generated_at=datetime.now(timezone.utc),
        signal_id=signal_id,
    )


def _make_trade_params(
    *,
    entry_price: float = 2650.0,
    atr_value: float = 3.5,
) -> TradeParameters:
    return TradeParameters(
        entry_price=entry_price,
        stop_loss=entry_price - 1.2 * atr_value,
        take_profit=entry_price + 2.5 * atr_value,
        position_size=0.05,
        risk_reward_ratio=2.08,
        atr_value=atr_value,
        sl_distance=1.2 * atr_value,
        tp_distance=2.5 * atr_value,
    )


@dataclass
class FakeQuote:
    symbol: str = "XAUUSD"
    bid: float = 2649.80
    ask: float = 2650.10
    last: float = 2650.0
    volume: float = 100.0
    time: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc)


class FakeMarketService:
    def __init__(self, quote: Optional[FakeQuote] = None):
        self._quote = quote or FakeQuote()

    def get_quote(self, symbol: str) -> Optional[FakeQuote]:
        return self._quote

    def set_quote_price(self, bid: float, ask: float) -> None:
        self._quote.bid = bid
        self._quote.ask = ask

    def get_symbol_point(self, symbol: str) -> Optional[float]:
        return 0.01


# ── compute_entry_zone tests ─────────────────────────────────────────────


class TestComputeEntryZone:
    def test_pullback_buy(self) -> None:
        config = PendingEntryConfig()
        low, high = compute_entry_zone(
            action="buy",
            close_price=2650.0,
            atr=3.5,
            zone_mode="pullback",
            config=config,
        )
        # BUY pullback: low = close - 0.3*ATR, high = close + 0.1*ATR
        assert low == pytest.approx(2650.0 - 0.3 * 3.5, abs=0.01)
        assert high == pytest.approx(2650.0 + 0.1 * 3.5, abs=0.01)

    def test_pullback_sell(self) -> None:
        config = PendingEntryConfig()
        low, high = compute_entry_zone(
            action="sell",
            close_price=2650.0,
            atr=3.5,
            zone_mode="pullback",
            config=config,
        )
        # SELL pullback: low = close - 0.1*ATR, high = close + 0.3*ATR
        assert low == pytest.approx(2650.0 - 0.1 * 3.5, abs=0.01)
        assert high == pytest.approx(2650.0 + 0.3 * 3.5, abs=0.01)

    def test_momentum_buy(self) -> None:
        config = PendingEntryConfig()
        low, high = compute_entry_zone(
            action="buy",
            close_price=2650.0,
            atr=3.5,
            zone_mode="momentum",
            config=config,
        )
        # BUY momentum: low = close - 0.1*ATR, high = close + 0.5*ATR
        assert low == pytest.approx(2650.0 - 0.1 * 3.5, abs=0.01)
        assert high == pytest.approx(2650.0 + 0.5 * 3.5, abs=0.01)

    def test_symmetric(self) -> None:
        config = PendingEntryConfig()
        low, high = compute_entry_zone(
            action="buy",
            close_price=2650.0,
            atr=3.5,
            zone_mode="symmetric",
            config=config,
        )
        # Symmetric: close ± 0.4*ATR
        assert low == pytest.approx(2650.0 - 0.4 * 3.5, abs=0.01)
        assert high == pytest.approx(2650.0 + 0.4 * 3.5, abs=0.01)

    def test_strategy_override(self) -> None:
        config = PendingEntryConfig(
            strategy_overrides={"supertrend": {"pullback_atr_factor": 0.5}}
        )
        low, high = compute_entry_zone(
            action="buy",
            close_price=2650.0,
            atr=3.5,
            zone_mode="pullback",
            config=config,
            strategy_name="supertrend",
        )
        # Overridden pullback: 0.5 instead of 0.3
        assert low == pytest.approx(2650.0 - 0.5 * 3.5, abs=0.01)


class TestComputeTimeout:
    def test_m5_timeout(self) -> None:
        config = PendingEntryConfig()
        td = compute_timeout("M5", config)
        assert td == timedelta(seconds=2.0 * 300)  # 2 bars × 300s

    def test_default_timeout(self) -> None:
        config = PendingEntryConfig()
        td = compute_timeout("W1", config)  # unknown tf
        assert td == timedelta(seconds=2.0 * 300)  # default_timeout_bars × M5 default


class TestCategoryZoneMode:
    def test_trend_is_pullback(self) -> None:
        assert _CATEGORY_ZONE_MODE["trend"] == "pullback"

    def test_reversion_is_symmetric(self) -> None:
        assert _CATEGORY_ZONE_MODE["reversion"] == "symmetric"

    def test_breakout_is_momentum(self) -> None:
        assert _CATEGORY_ZONE_MODE["breakout"] == "momentum"


# ── PendingEntryManager tests ─────────────────────────────────────────────


class TestPendingEntryManager:
    def _make_manager(
        self,
        *,
        market_service: Optional[FakeMarketService] = None,
        execute_fn: Optional[Any] = None,
        on_expired_fn: Optional[Any] = None,
    ) -> PendingEntryManager:
        return PendingEntryManager(
            config=PendingEntryConfig(check_interval=0.05),
            market_service=market_service or FakeMarketService(),
            execute_fn=execute_fn or MagicMock(),
            on_expired_fn=on_expired_fn,
        )

    def _make_pending(
        self,
        *,
        signal_id: str = "sig-001",
        action: str = "buy",
        entry_low: float = 2649.0,
        entry_high: float = 2651.0,
        timeout_seconds: float = 60.0,
        category: str = "trend",
    ) -> PendingEntry:
        now = datetime.now(timezone.utc)
        return PendingEntry(
            signal_event=_make_signal_event(
                signal_id=signal_id, action=action, category=category,
            ),
            trade_params=_make_trade_params(),
            cost_metrics={},
            entry_low=entry_low,
            entry_high=entry_high,
            reference_price=2650.0,
            created_at=now,
            expires_at=now + timedelta(seconds=timeout_seconds),
            zone_mode="pullback",
        )

    def test_submit_and_active_count(self) -> None:
        mgr = self._make_manager()
        pending = self._make_pending()
        mgr.submit(pending)
        assert mgr.active_count() == 1
        assert mgr._stats["total_submitted"] == 1

    def test_cancel(self) -> None:
        mgr = self._make_manager()
        pending = self._make_pending()
        mgr.submit(pending)
        assert mgr.cancel("sig-001", "test")
        assert mgr.active_count() == 0
        assert mgr._stats["total_cancelled"] == 1

    def test_cancel_nonexistent(self) -> None:
        mgr = self._make_manager()
        assert not mgr.cancel("nonexistent")

    def test_cancel_by_symbol(self) -> None:
        mgr = self._make_manager()
        mgr.submit(self._make_pending(signal_id="s1"))
        mgr.submit(self._make_pending(signal_id="s2"))
        cancelled = mgr.cancel_by_symbol("XAUUSD", "override")
        assert cancelled == 2
        assert mgr.active_count() == 0

    def test_cancel_by_symbol_exclude_direction(self) -> None:
        mgr = self._make_manager()
        mgr.submit(self._make_pending(signal_id="s1", action="buy"))
        mgr.submit(self._make_pending(signal_id="s2", action="sell"))
        cancelled = mgr.cancel_by_symbol("XAUUSD", "override", exclude_direction="buy")
        assert cancelled == 1  # only sell cancelled
        assert mgr.active_count() == 1

    def test_fill_when_price_in_zone(self) -> None:
        execute_fn = MagicMock()
        market = FakeMarketService(FakeQuote(ask=2650.50, bid=2650.20))
        mgr = self._make_manager(market_service=market, execute_fn=execute_fn)

        pending = self._make_pending(entry_low=2649.0, entry_high=2651.0)
        mgr.submit(pending)

        # Start and let it check
        mgr.start()
        time.sleep(0.2)
        mgr.shutdown()

        execute_fn.assert_called_once()
        assert mgr._stats["total_filled"] == 1

    def test_no_fill_when_price_outside_zone(self) -> None:
        execute_fn = MagicMock()
        market = FakeMarketService(FakeQuote(ask=2655.0, bid=2654.50))
        mgr = self._make_manager(market_service=market, execute_fn=execute_fn)

        pending = self._make_pending(entry_low=2649.0, entry_high=2651.0)
        mgr.submit(pending)

        mgr.start()
        time.sleep(0.2)
        mgr.shutdown()

        execute_fn.assert_not_called()
        assert mgr._stats["total_filled"] == 0

    def test_expire_when_timeout(self) -> None:
        execute_fn = MagicMock()
        on_expired_fn = MagicMock()
        market = FakeMarketService(FakeQuote(ask=2655.0, bid=2654.50))
        mgr = self._make_manager(
            market_service=market,
            execute_fn=execute_fn,
            on_expired_fn=on_expired_fn,
        )

        # 超短超时
        pending = self._make_pending(
            entry_low=2649.0, entry_high=2651.0, timeout_seconds=0.05,
        )
        mgr.submit(pending)

        mgr.start()
        time.sleep(0.3)
        mgr.shutdown()

        execute_fn.assert_not_called()
        on_expired_fn.assert_called_once()
        assert mgr._stats["total_expired"] == 1

    def test_sell_uses_bid_price(self) -> None:
        execute_fn = MagicMock()
        # bid is in zone, ask is not
        market = FakeMarketService(FakeQuote(ask=2660.0, bid=2650.50))
        mgr = self._make_manager(market_service=market, execute_fn=execute_fn)

        pending = self._make_pending(
            action="sell", entry_low=2649.0, entry_high=2651.0,
        )
        mgr.submit(pending)

        mgr.start()
        time.sleep(0.2)
        mgr.shutdown()

        execute_fn.assert_called_once()

    def test_spread_check_blocks(self) -> None:
        execute_fn = MagicMock()
        # Price in zone but spread very wide: ask-bid = 1.0, point = 0.01 → 100 points
        market = FakeMarketService(FakeQuote(ask=2650.50, bid=2649.50))
        mgr = PendingEntryManager(
            config=PendingEntryConfig(check_interval=0.05, max_spread_points=50.0),
            market_service=market,
            execute_fn=execute_fn,
        )

        pending = self._make_pending(entry_low=2649.0, entry_high=2651.0)
        mgr.submit(pending)

        mgr.start()
        time.sleep(0.2)
        mgr.shutdown()

        execute_fn.assert_not_called()

    def test_status_returns_correct_structure(self) -> None:
        mgr = self._make_manager()
        pending = self._make_pending()
        mgr.submit(pending)

        status = mgr.status()
        assert status["active_count"] == 1
        assert len(status["entries"]) == 1
        assert "stats" in status
        assert status["stats"]["total_submitted"] == 1

    def test_best_price_tracked_for_buy(self) -> None:
        market = FakeMarketService(FakeQuote(ask=2655.0, bid=2654.50))
        mgr = self._make_manager(market_service=market)

        pending = self._make_pending(entry_low=2649.0, entry_high=2651.0)
        mgr.submit(pending)

        # Manually trigger check
        mgr._check_all_entries()

        # best_price_seen for buy = min ask seen
        assert pending.best_price_seen == 2655.0
        assert pending.checks_count == 1

    def test_shutdown_clears_pending(self) -> None:
        mgr = self._make_manager()
        mgr.submit(self._make_pending(signal_id="s1"))
        mgr.submit(self._make_pending(signal_id="s2"))
        mgr.shutdown()
        assert mgr.active_count() == 0
