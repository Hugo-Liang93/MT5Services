"""Tests for PositionManager hardening: flash-disconnect guard, partial close,
live PnL, lock safety, and new-format comment restoration.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.trading.closeout import (
    ExposureCloseoutController,
    ExposureCloseoutService,
)
from src.trading.positions import PositionManager, TrackedPosition
from src.trading.execution.sizing import TradeParameters


def _params(**overrides) -> TradeParameters:
    defaults = dict(
        entry_price=3000.0,
        stop_loss=2990.0,
        take_profit=3020.0,
        position_size=0.1,
        atr_value=5.0,
        risk_reward_ratio=2.0,
        sl_distance=10.0,
        tp_distance=20.0,
    )
    defaults.update(overrides)
    return TradeParameters(**defaults)


class FailingTradingModule:
    """Simulates MT5 connection failure for specific symbols."""

    def __init__(self, positions=None, fail_symbols=None):
        self._positions = list(positions or [])
        self._fail_symbols = set(fail_symbols or [])

    def get_positions(self, symbol=None):
        if symbol in self._fail_symbols:
            raise ConnectionError(f"MT5 connection lost for {symbol}")
        if symbol is None:
            return list(self._positions)
        return [p for p in self._positions if getattr(p, "symbol", None) == symbol]

    def get_orders(self, symbol=None, magic=None):
        return []

    def close_all_positions(self, **kwargs):
        self._positions = []
        return {"closed": [], "failed": []}

    def cancel_orders_by_tickets(self, tickets):
        return {"canceled": list(tickets), "failed": []}

    def get_position_close_details(self, ticket, *, symbol=None, lookback_days=7):
        return None

    def modify_positions(self, **kwargs):
        return {"modified": [kwargs.get("ticket")], "failed": []}


def _manager(trading: FailingTradingModule, **kwargs) -> PositionManager:
    return PositionManager(
        trading_module=trading,
        end_of_day_closeout=ExposureCloseoutController(
            ExposureCloseoutService(trading)
        ),
        **kwargs,
    )


# ── Flash-disconnect guard ────────────────────────────────────────


def test_reconcile_skips_symbols_with_failed_queries() -> None:
    """When get_positions() fails for a symbol, tracked positions for that
    symbol must NOT be removed (they might still be open in MT5)."""
    trading = FailingTradingModule(fail_symbols={"XAUUSD"})
    manager = _manager(trading)
    closed = []
    manager.add_close_callback(lambda pos, price: closed.append(pos.ticket))

    manager.track_position(
        ticket=100, signal_id="sig-1", symbol="XAUUSD", action="buy", params=_params(),
    )

    manager._reconcile_with_mt5()

    # Position must still be tracked — not falsely removed
    assert len(manager.active_positions()) == 1
    assert closed == []


def test_reconcile_removes_closed_positions_for_healthy_symbols() -> None:
    """Positions for symbols where get_positions() succeeds and returns
    empty should still be detected as closed."""
    trading = FailingTradingModule(positions=[], fail_symbols=set())
    manager = _manager(trading)
    closed = []
    manager.add_close_callback(lambda pos, price: closed.append(pos.ticket))

    manager.track_position(
        ticket=200, signal_id="sig-2", symbol="XAUUSD", action="sell", params=_params(),
    )

    manager._reconcile_with_mt5()

    assert len(manager.active_positions()) == 0
    assert closed == [200]


# ── Partial close detection ───────────────────────────────────────


def test_reconcile_detects_partial_close() -> None:
    """When MT5 reports a smaller volume than tracked, the tracked position
    should be updated to match."""
    raw_pos = SimpleNamespace(
        ticket=300, symbol="XAUUSD", volume=0.05, price_current=3010.0,
    )
    trading = FailingTradingModule(positions=[raw_pos])
    manager = _manager(trading)

    manager.track_position(
        ticket=300, signal_id="sig-3", symbol="XAUUSD", action="buy",
        params=_params(position_size=0.10),
    )
    assert manager.active_positions()[0]["volume"] == 0.10

    manager._reconcile_with_mt5()

    assert manager.active_positions()[0]["volume"] == 0.05


# ── Live PnL in active_positions ──────────────────────────────────


def test_active_positions_includes_unrealized_pnl() -> None:
    raw_pos = SimpleNamespace(
        ticket=400, symbol="XAUUSD", volume=0.10, price_current=3015.0,
    )
    trading = FailingTradingModule(positions=[raw_pos])
    manager = _manager(trading)

    manager.track_position(
        ticket=400, signal_id="sig-4", symbol="XAUUSD", action="buy",
        params=_params(entry_price=3000.0),
    )

    manager._reconcile_with_mt5()

    pos = manager.active_positions()[0]
    assert pos["current_price"] == 3015.0
    assert pos["unrealized_pnl"] == 15.0


def test_active_positions_unrealized_pnl_sell() -> None:
    raw_pos = SimpleNamespace(
        ticket=401, symbol="XAUUSD", volume=0.10, price_current=2990.0,
    )
    trading = FailingTradingModule(positions=[raw_pos])
    manager = _manager(trading)

    manager.track_position(
        ticket=401, signal_id="sig-5", symbol="XAUUSD", action="sell",
        params=_params(entry_price=3000.0),
    )

    manager._reconcile_with_mt5()

    pos = manager.active_positions()[0]
    assert pos["current_price"] == 2990.0
    assert pos["unrealized_pnl"] == 10.0  # (3000 - 2990) for sell


# ── close_source field ────────────────────────────────────────────


def test_close_source_set_on_reconcile_removal() -> None:
    """When reconcile removes a position, close_source should be set on the
    TrackedPosition passed to callbacks."""
    trading = FailingTradingModule(positions=[])
    manager = _manager(trading)
    received_source = []
    manager.add_close_callback(lambda pos, price: received_source.append(pos.close_source))

    manager.track_position(
        ticket=500, signal_id="sig-6", symbol="XAUUSD", action="buy", params=_params(),
    )

    manager._reconcile_with_mt5()

    assert received_source == ["mt5_missing"]


# ── New comment format restoration ────────────────────────────────


def test_sync_restores_positions_with_timeframe_comment_prefix() -> None:
    """The new comment format '{tf}:{strategy}:{direction}' should be
    recognized as restorable."""
    raw_pos = SimpleNamespace(
        ticket=600, symbol="XAUUSD", volume=0.1, price_open=3000.0,
        sl=2990.0, tp=3020.0,
        time=datetime(2026, 3, 26, 12, 0, tzinfo=timezone.utc),
        type=0,
        comment="M15:trend_vote:buy",
    )
    trading = FailingTradingModule(positions=[raw_pos])
    manager = _manager(trading)

    result = manager.sync_open_positions()

    assert result["synced"] == 1
    assert result["skipped"] == 0
    active = manager.active_positions()
    assert active[0]["ticket"] == 600


def test_sync_still_skips_unknown_comments() -> None:
    raw_pos = SimpleNamespace(
        ticket=601, symbol="XAUUSD", volume=0.1, price_open=3000.0,
        sl=2990.0, tp=3020.0,
        time=datetime(2026, 3, 26, 12, 0, tzinfo=timezone.utc),
        type=0,
        comment="manual_trade_by_user",
    )
    trading = FailingTradingModule(positions=[raw_pos])
    manager = _manager(trading)

    result = manager.sync_open_positions()

    assert result["skipped"] == 1
    assert manager.active_positions() == []


def test_reconcile_recovers_new_open_positions_before_price_management() -> None:
    raw_pos = SimpleNamespace(
        ticket=602,
        symbol="XAUUSD",
        volume=0.1,
        price_open=3000.0,
        price_current=3012.0,
        sl=2990.0,
        tp=3020.0,
        time=datetime(2026, 3, 26, 12, 0, tzinfo=timezone.utc),
        type=0,
        comment="M15:trend_vote:buy",
    )
    trading = FailingTradingModule(positions=[raw_pos])
    manager = _manager(trading)

    manager._reconcile_with_mt5()

    active = manager.active_positions()
    assert len(active) == 1
    assert active[0]["ticket"] == 602
    assert active[0]["current_price"] == 3012.0
