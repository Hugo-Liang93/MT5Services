from __future__ import annotations

from src.app_runtime.mode_policy import (
    RuntimeMode,
    RuntimeModeAutoTransitionPolicy,
    RuntimeModeEODAction,
    RuntimeModeTransitionGuard,
)


class _TradeModule:
    def __init__(self, *, positions=None, orders=None, fail: bool = False) -> None:
        self._positions = list(positions or [])
        self._orders = list(orders or [])
        self._fail = fail

    def get_positions(self):
        if self._fail:
            raise RuntimeError("positions unavailable")
        return list(self._positions)

    def get_orders(self):
        if self._fail:
            raise RuntimeError("orders unavailable")
        return list(self._orders)


def test_transition_guard_allows_ingest_only_only_when_book_is_empty() -> None:
    empty_guard = RuntimeModeTransitionGuard(
        trading_module_getter=lambda: _TradeModule(),
    )
    busy_guard = RuntimeModeTransitionGuard(
        trading_module_getter=lambda: _TradeModule(positions=[{"ticket": 1}]),
    )

    assert empty_guard.can_enter(RuntimeMode.INGEST_ONLY) is True
    assert busy_guard.can_enter(RuntimeMode.INGEST_ONLY) is False


def test_transition_guard_fails_closed_when_trade_state_cannot_be_read() -> None:
    guard = RuntimeModeTransitionGuard(
        trading_module_getter=lambda: _TradeModule(fail=True),
    )

    assert guard.can_enter_ingest_only() is False


def test_auto_transition_policy_falls_back_to_risk_off_when_ingest_only_is_unsafe() -> None:
    policy = RuntimeModeAutoTransitionPolicy(
        after_eod_action=RuntimeModeEODAction.INGEST_ONLY
    )

    assert policy.resolve_after_eod(
        current_mode=RuntimeMode.FULL,
        after_eod_today=True,
        can_enter_ingest_only=False,
    ) == RuntimeMode.RISK_OFF


def test_auto_transition_policy_noops_outside_active_modes_or_before_eod() -> None:
    policy = RuntimeModeAutoTransitionPolicy(
        after_eod_action=RuntimeModeEODAction.RISK_OFF
    )

    assert (
        policy.resolve_after_eod(
            current_mode=RuntimeMode.RISK_OFF,
            after_eod_today=True,
            can_enter_ingest_only=True,
        )
        is None
    )
    assert (
        policy.resolve_after_eod(
            current_mode=RuntimeMode.FULL,
            after_eod_today=False,
            can_enter_ingest_only=True,
        )
        is None
    )
