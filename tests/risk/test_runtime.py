from __future__ import annotations

from types import SimpleNamespace

from src.risk.runtime import wire_margin_guard


class DummyTradeModule:
    def __init__(self):
        self.closed_positions: list[tuple[int, str]] = []
        self.closed_all_comments: list[str] = []
        self.positions = [
            SimpleNamespace(ticket=11, profit=-20.0),
            SimpleNamespace(ticket=12, profit=5.0),
        ]

    def get_positions(self, symbol=None):
        return list(self.positions)

    def close_position(self, ticket: int, **kwargs):
        self.closed_positions.append((ticket, str(kwargs.get("comment") or "")))
        return {"ticket": ticket, "success": True}

    def close_all_positions(self, **kwargs):
        self.closed_all_comments.append(str(kwargs.get("comment") or ""))
        return {"closed": [11, 12], "failed": []}


class DummyPositionManager:
    def __init__(self):
        self.trailing_atr_multiplier = 3.0
        self.tighten_calls: list[float] = []
        self.margin_guard = None

    def set_margin_guard(self, guard):
        self.margin_guard = guard

    def tighten_trailing_stops(self, factor: float) -> int:
        self.tighten_calls.append(factor)
        self.trailing_atr_multiplier *= factor
        return 4


class DummyExecutor:
    def __init__(self):
        self.margin_guard = None

    def set_margin_guard(self, guard):
        self.margin_guard = guard


def test_wire_margin_guard_binds_to_explicit_ports() -> None:
    position_manager = DummyPositionManager()
    trade_module = DummyTradeModule()
    executor = DummyExecutor()

    guard = wire_margin_guard(
        position_manager=position_manager,
        trade_module=trade_module,
        trade_executor=executor,
    )

    assert guard is not None
    assert position_manager.margin_guard is guard
    assert executor.margin_guard is guard

    actions = guard.act(
        guard.evaluate(
            equity=1000.0,
            margin=800.0,
            free_margin=50.0,
        )
    )

    assert position_manager.tighten_calls == [guard.config.tighten_stops_factor]
    assert any(action.startswith("tighten_stops:") for action in actions)


def test_margin_guard_worst_first_uses_trade_port_close_position() -> None:
    position_manager = DummyPositionManager()
    trade_module = DummyTradeModule()

    guard = wire_margin_guard(
        position_manager=position_manager,
        trade_module=trade_module,
    )

    assert guard is not None
    actions = guard.act(
        guard.evaluate(
            equity=500.0,
            margin=1000.0,
            free_margin=10.0,
        )
    )

    assert trade_module.closed_positions == [(11, "margin_guard_emergency")]
    assert any(action.startswith("emergency_close_worst:") for action in actions)
