from __future__ import annotations

from datetime import datetime, timezone

from src.trading.position_manager import PositionManager


class DummyTradingModule:
    def __init__(self, positions=None):
        self._positions = list(positions or [])
        self.close_all_calls = []

    def get_positions(self, symbol=None):
        return list(self._positions)

    def close_all_positions(self, **kwargs):
        self.close_all_calls.append(kwargs)
        self._positions = []
        return {"closed": [1], "failed": []}


def test_position_manager_runs_end_of_day_closeout_once_per_day() -> None:
    trading = DummyTradingModule(positions=[{"ticket": 1}])
    manager = PositionManager(
        trading_module=trading,
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

    assert first == {"closed": [1], "failed": []}
    assert second is None
    assert trading.close_all_calls == [{"comment": "end_of_day_closeout"}]
    assert manager.status()["last_end_of_day_close_date"] == "2026-03-20"


def test_position_manager_does_not_close_before_cutoff() -> None:
    trading = DummyTradingModule(positions=[{"ticket": 1}])
    manager = PositionManager(
        trading_module=trading,
        end_of_day_close_enabled=True,
        end_of_day_close_hour_utc=21,
        end_of_day_close_minute_utc=0,
    )

    result = manager._run_end_of_day_closeout(
        datetime(2026, 3, 20, 20, 59, tzinfo=timezone.utc)
    )

    assert result is None
    assert trading.close_all_calls == []
