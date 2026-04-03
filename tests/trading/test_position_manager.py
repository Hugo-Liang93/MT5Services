from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.trading.closeout import (
    ExposureCloseoutController,
    ExposureCloseoutService,
)
from src.trading.positions import PositionManager
from src.trading.execution import TradeParameters


class DummyTradingModule:
    def __init__(self, positions=None, orders=None):
        self._positions = list(positions or [])
        self._orders = list(orders or [])
        self.close_all_calls = []
        self.cancel_calls = []
        self.modify_calls = []
        self.close_details = {}
        self.modify_result = None

    def get_positions(self, symbol=None):
        if symbol is None:
            return list(self._positions)
        return [row for row in self._positions if getattr(row, "symbol", None) == symbol]

    def get_orders(self, symbol=None, magic=None):
        return list(self._orders)

    def close_all_positions(self, **kwargs):
        self.close_all_calls.append(kwargs)
        self._positions = []
        return {"closed": [1], "failed": []}

    def cancel_orders_by_tickets(self, tickets):
        self.cancel_calls.append(list(tickets))
        self._orders = []
        return {"canceled": list(tickets), "failed": []}

    def modify_positions(self, **kwargs):
        self.modify_calls.append(kwargs)
        if self.modify_result is not None:
            return self.modify_result
        return {"modified": [kwargs.get("ticket")], "failed": []}

    def get_position_close_details(self, ticket: int, *, symbol=None, lookback_days: int = 7):
        return self.close_details.get(ticket)


def _manager(trading: DummyTradingModule, **kwargs) -> PositionManager:
    return PositionManager(
        trading_module=trading,
        end_of_day_closeout=ExposureCloseoutController(
            ExposureCloseoutService(trading)
        ),
        **kwargs,
    )


def test_position_manager_runs_end_of_day_closeout_once_per_day() -> None:
    trading = DummyTradingModule(
        positions=[{"ticket": 1}],
        orders=[SimpleNamespace(ticket=11)],
    )
    manager = _manager(
        trading,
        end_of_day_close_enabled=True,
        end_of_day_close_hour_utc=21,
        end_of_day_close_minute_utc=0,
    )

    first = manager._run_end_of_day_closeout(
        datetime(2026, 3, 20, 21, 1, tzinfo=timezone.utc)
    )
    second = manager._run_end_of_day_closeout(
        datetime(2026, 3, 20, 21, 5, tzinfo=timezone.utc)
    )

    assert first["completed"] is True
    assert first["positions"]["completed"] == [1]
    assert first["orders"]["completed"] == [11]
    assert second is None
    assert trading.close_all_calls == [{"comment": "end_of_day_closeout"}]
    assert trading.cancel_calls == [[11]]
    assert manager.status()["last_end_of_day_gate_date"] == "2026-03-20"
    assert manager.status()["last_end_of_day_close_date"] == "2026-03-20"


def test_position_manager_exposes_margin_guard_status_via_public_api() -> None:
    trading = DummyTradingModule()
    manager = _manager(trading)

    class _Guard:
        @staticmethod
        def status():
            return {"state": "warn", "margin_level_pct": 180.0}

    manager.set_margin_guard(_Guard())

    assert manager.margin_guard_status() == {
        "state": "warn",
        "margin_level_pct": 180.0,
    }
    assert manager.status()["margin_guard"] == {
        "state": "warn",
        "margin_level_pct": 180.0,
    }


def test_position_manager_tighten_trailing_stops_uses_public_method() -> None:
    trading = DummyTradingModule()
    manager = _manager(trading, trailing_atr_multiplier=3.0)
    manager._positions = {
        1: {"ticket": 1},
        2: {"ticket": 2},
    }

    count = manager.tighten_trailing_stops(0.5)

    assert count == 2
    assert manager.trailing_atr_multiplier == 1.5


def test_position_manager_does_not_close_before_cutoff() -> None:
    trading = DummyTradingModule(positions=[{"ticket": 1}])
    manager = _manager(
        trading,
        end_of_day_close_enabled=True,
        end_of_day_close_hour_utc=21,
        end_of_day_close_minute_utc=0,
    )

    result = manager._run_end_of_day_closeout(
        datetime(2026, 3, 20, 20, 59, tzinfo=timezone.utc)
    )

    assert result is None
    assert trading.close_all_calls == []
    assert trading.cancel_calls == []


def test_position_manager_sync_open_positions_restores_signal_context() -> None:
    trading = DummyTradingModule(
        positions=[
            SimpleNamespace(
                ticket=101,
                symbol="XAUUSD",
                volume=0.1,
                price_open=3000.0,
                sl=2990.0,
                tp=3020.0,
                time=datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc),
                type=0,
                comment="auto:sma:buy_rsigrest1",
            )
        ]
    )
    manager = _manager(trading)
    recovered = []
    manager.set_recovery_hooks(
        position_context_resolver=lambda ticket, comment: {
            "signal_id": "sig-restored",
            "timeframe": "M5",
            "strategy": "sma_trend",
            "confidence": 0.91,
            "regime": "trend",
            "fill_price": 3000.2,
            "comment": comment,
            "source": "audit_recovery",
        },
        recovered_position_callback=lambda pos: recovered.append(pos.signal_id),
    )

    result = manager.sync_open_positions()

    assert result == {"synced": 1, "recovered": 1, "skipped": 0}
    active = manager.active_positions()
    assert active[0]["signal_id"] == "sig-restored"
    assert active[0]["strategy"] == "sma_trend"
    assert active[0]["entry_price"] == 3000.2
    assert recovered == ["sig-restored"]


def test_position_manager_sync_open_positions_skips_manual_positions_without_context() -> None:
    trading = DummyTradingModule(
        positions=[
            SimpleNamespace(
                ticket=111,
                symbol="XAUUSD",
                volume=0.1,
                price_open=3000.0,
                sl=2990.0,
                tp=3020.0,
                time=datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc),
                type=0,
                comment="manual_trade",
            )
        ]
    )
    manager = _manager(trading)

    result = manager.sync_open_positions()

    assert result == {"synced": 0, "recovered": 0, "skipped": 1}
    assert manager.active_positions() == []


def test_position_manager_sync_open_positions_restores_agent_comment_without_audit_context() -> None:
    trading = DummyTradingModule(
        positions=[
            SimpleNamespace(
                ticket=112,
                symbol="XAUUSD",
                volume=0.1,
                price_open=3001.0,
                sl=2991.0,
                tp=3021.0,
                time=datetime(2026, 3, 20, 12, 5, tzinfo=timezone.utc),
                type=0,
                comment="agent:consensus:buy:sig_1",
            )
        ]
    )
    manager = _manager(trading)

    result = manager.sync_open_positions()

    assert result == {"synced": 1, "recovered": 0, "skipped": 0}
    active = manager.active_positions()
    assert active[0]["signal_id"] == "restored:112"
    assert active[0]["comment"] == "agent:consensus:buy:sig_1"


def test_position_manager_sync_open_positions_prefers_persisted_runtime_state() -> None:
    trading = DummyTradingModule(
        positions=[
            SimpleNamespace(
                ticket=113,
                symbol="XAUUSD",
                volume=0.1,
                price_open=3001.0,
                sl=2991.0,
                tp=3021.0,
                time=datetime(2026, 3, 20, 12, 5, tzinfo=timezone.utc),
                type=0,
                comment="manual_trade",
            )
        ]
    )
    manager = _manager(trading)
    manager.set_recovery_hooks(
        position_state_resolver=lambda ticket: {
            "signal_id": "sig-persisted",
            "timeframe": "M15",
            "strategy": "trend_vote",
            "confidence": 0.88,
            "regime": "trend",
            "entry_price": 3000.5,
            "atr_at_entry": 4.2,
            "highest_price": 3015.0,
            "current_price": 3012.0,
            "breakeven_applied": True,
            "trailing_active": True,
            "comment": "M15:trend_vote:buy",
            "source": "position_runtime_state",
        },
    )

    result = manager.sync_open_positions()

    assert result == {"synced": 1, "recovered": 0, "skipped": 0}
    active = manager.active_positions()
    assert active[0]["signal_id"] == "sig-persisted"
    assert active[0]["strategy"] == "trend_vote"
    assert active[0]["entry_price"] == 3000.5
    assert active[0]["breakeven_applied"] is True
    assert active[0]["trailing_active"] is True
    assert active[0]["highest_price"] == 3015.0


def test_position_manager_reconcile_uses_real_close_price_from_history() -> None:
    trading = DummyTradingModule(positions=[])
    trading.close_details[202] = {"close_price": 3012.4}
    manager = _manager(trading)
    closed = []
    manager.add_close_callback(lambda pos, close_price: closed.append((pos.ticket, close_price)))
    manager.track_position(
        ticket=202,
        signal_id="sig-close",
        symbol="XAUUSD",
        action="buy",
        params=TradeParameters(
            entry_price=3000.0,
            stop_loss=2990.0,
            take_profit=3020.0,
            position_size=0.1,
            atr_value=5.0,
            risk_reward_ratio=2.0,
            sl_distance=10.0,
            tp_distance=20.0,
        ),
    )

    manager._reconcile_with_mt5()

    assert closed == [(202, 3012.4)]
    assert manager.active_positions() == []


def test_position_manager_modify_sl_targets_single_ticket() -> None:
    trading = DummyTradingModule(positions=[])
    manager = _manager(trading)
    pos = manager.track_position(
        ticket=303,
        signal_id="sig-modify",
        symbol="XAUUSD",
        action="buy",
        params=TradeParameters(
            entry_price=3000.0,
            stop_loss=2990.0,
            take_profit=3020.0,
            position_size=0.1,
            atr_value=5.0,
            risk_reward_ratio=2.0,
            sl_distance=10.0,
            tp_distance=20.0,
        ),
    )

    assert manager._modify_sl(pos, 3001.25) is True
    assert trading.modify_calls[-1] == {"ticket": 303, "symbol": "XAUUSD", "sl": 3001.25}
    assert pos.stop_loss == 3001.25


def test_position_manager_does_not_mark_breakeven_when_modify_fails() -> None:
    trading = DummyTradingModule(positions=[])
    trading.modify_result = {"modified": [], "failed": [{"ticket": 404, "error": "position_not_found"}]}
    manager = _manager(
        trading,
        breakeven_atr_threshold=1.0,
    )
    manager.track_position(
        ticket=404,
        signal_id="sig-breakeven",
        symbol="XAUUSD",
        action="buy",
        params=TradeParameters(
            entry_price=3000.0,
            stop_loss=2990.0,
            take_profit=3020.0,
            position_size=0.1,
            atr_value=5.0,
            risk_reward_ratio=2.0,
            sl_distance=10.0,
            tp_distance=20.0,
        ),
    )

    manager.update_price(404, 3005.5)

    active = manager.active_positions()
    assert active[0]["breakeven_applied"] is False


def test_position_manager_keeps_eod_gate_and_retries_when_orders_remain() -> None:
    class StickyOrderTradingModule(DummyTradingModule):
        def cancel_orders_by_tickets(self, tickets):
            self.cancel_calls.append(list(tickets))
            return {"canceled": [], "failed": [{"ticket": tickets[0], "error": "market_closed"}]}

    trading = StickyOrderTradingModule(
        positions=[],
        orders=[SimpleNamespace(ticket=77)],
    )
    manager = _manager(
        trading,
        end_of_day_close_enabled=True,
        end_of_day_close_hour_utc=21,
        end_of_day_close_minute_utc=0,
    )

    first = manager._run_end_of_day_closeout(
        datetime(2026, 3, 20, 21, 1, tzinfo=timezone.utc)
    )
    second = manager._run_end_of_day_closeout(
        datetime(2026, 3, 20, 21, 2, tzinfo=timezone.utc)
    )

    assert first["completed"] is False
    assert second["completed"] is False
    assert manager.status()["last_end_of_day_gate_date"] == "2026-03-20"
    assert manager.status()["last_end_of_day_close_date"] is None
    assert trading.cancel_calls == [[77], [77]]
