from __future__ import annotations

from types import SimpleNamespace

from src.trading.exposure_closeout import ExposureCloseoutService


class DummyExposureTrading:
    def __init__(self, positions=None, orders=None):
        self.positions = list(positions or [])
        self.orders = list(orders or [])
        self.close_calls = []
        self.cancel_calls = []

    def get_positions(self, symbol=None, magic=None):
        return list(self.positions)

    def get_orders(self, symbol=None, magic=None):
        return list(self.orders)

    def close_all_positions(self, **kwargs):
        self.close_calls.append(kwargs)
        self.positions = []
        return {"closed": [101], "failed": []}

    def cancel_orders_by_tickets(self, tickets):
        self.cancel_calls.append(list(tickets))
        self.orders = []
        return {"canceled": list(tickets), "failed": []}


def test_exposure_closeout_service_closes_positions_and_cancels_orders() -> None:
    trading = DummyExposureTrading(
        positions=[SimpleNamespace(ticket=101)],
        orders=[SimpleNamespace(ticket=201), SimpleNamespace(ticket=202)],
    )
    service = ExposureCloseoutService(trading)

    result = service.execute(comment="end_of_day_closeout")

    assert result.completed is True
    assert result.positions.completed_tickets == [101]
    assert result.orders.completed_tickets == [201, 202]
    assert trading.close_calls == [{"comment": "end_of_day_closeout"}]
    assert trading.cancel_calls == [[201, 202]]


def test_exposure_closeout_service_reports_remaining_orders_when_cancel_fails() -> None:
    class StickyOrderTrading(DummyExposureTrading):
        def cancel_orders_by_tickets(self, tickets):
            self.cancel_calls.append(list(tickets))
            return {"canceled": [], "failed": [{"ticket": tickets[0], "error": "market_closed"}]}

    trading = StickyOrderTrading(orders=[SimpleNamespace(ticket=301)])
    service = ExposureCloseoutService(trading)

    result = service.execute(comment="end_of_day_closeout")

    assert result.completed is False
    assert result.remaining_order_tickets == [301]
    assert result.orders.failed == [{"ticket": 301, "error": "market_closed"}]
