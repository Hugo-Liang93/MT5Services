"""OCO 多 member + any-fill 撤 sibling + cancel_by_group 单元测试（ADR-013 P4）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.signals.models import SignalEvent
from src.trading.execution.sizing import TradeParameters
from src.trading.pending import PendingEntry, PendingEntryConfig, PendingEntryManager


@dataclass
class _StubMarketService:
    quotes: dict[str, Any] = None  # type: ignore[assignment]

    def get_latest_quote(self, symbol: str) -> Any:
        if self.quotes is None:
            return None
        return self.quotes.get(symbol)


class _StubCancellationPort:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def cancel_order(self, ticket: int, reason: str = "") -> bool:
        self.calls.append(("cancel_order", ticket))
        return True

    def cancel_orders_by_group(self, group_id: str) -> int:
        self.calls.append(("cancel_orders_by_group", group_id))
        return 0


def _signal_event(*, signal_id: str = "sig_1", direction: str = "buy") -> SignalEvent:
    return SignalEvent(
        symbol="XAUUSD",
        timeframe="M15",
        strategy="test_strategy",
        direction=direction,
        confidence=0.7,
        signal_state="confirmed_buy",
        scope="confirmed",
        indicators={"atr14": {"atr": 10.0}},
        metadata={},
        generated_at=datetime.now(timezone.utc),
        signal_id=signal_id,
        reason="test",
    )


def _trade_params() -> TradeParameters:
    return TradeParameters(
        entry_price=4500.0,
        stop_loss=4485.0,
        take_profit=4525.0,
        position_size=0.01,
        atr_value=10.0,
        sl_distance=15.0,
        tp_distance=25.0,
        risk_reward_ratio=1.67,
    )


def _pending_entry(
    *,
    signal_id: str = "sig_1",
    member_id: str = "limit_pullback",
    group_id: str = "grp_1",
    group_role: str = "limit",
    entry_low: float = 4498.0,
    entry_high: float = 4502.0,
) -> PendingEntry:
    event = _signal_event(signal_id=signal_id)
    entry_key = signal_id if member_id == "" else f"{signal_id}#{member_id}"
    return PendingEntry(
        signal_event=event,
        trade_params=_trade_params(),
        cost_metrics={},
        entry_low=entry_low,
        entry_high=entry_high,
        reference_price=4500.0,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        entry_key=entry_key,
        group_id=group_id,
        member_id=member_id,
        group_role=group_role,
    )


def _make_manager(execute_calls: list) -> PendingEntryManager:
    return PendingEntryManager(
        config=PendingEntryConfig(),
        market_service=_StubMarketService(quotes={}),
        cancellation_port=_StubCancellationPort(),
        execute_fn=lambda event, params, cost: execute_calls.append(
            (event.signal_id, params)
        ),
    )


class TestSubmitOcoGroup:
    def test_submit_two_members_indexes_under_group(self) -> None:
        mgr = _make_manager([])
        member_a = _pending_entry(member_id="limit_pullback", group_role="limit")
        member_b = _pending_entry(
            member_id="stop_breakout",
            group_role="stop",
            entry_low=4520.0,
            entry_high=4530.0,
        )
        mgr.submit_oco_group([member_a, member_b], group_id="grp_1")

        assert "grp_1" in mgr._groups
        assert len(mgr._groups["grp_1"]) == 2
        assert "sig_1#limit_pullback" in mgr._pending
        assert "sig_1#stop_breakout" in mgr._pending

    def test_duplicate_member_id_rejected(self) -> None:
        mgr = _make_manager([])
        a = _pending_entry(member_id="limit_pullback")
        b = _pending_entry(member_id="limit_pullback")  # duplicate
        with pytest.raises(ValueError, match="duplicate entry_keys"):
            mgr.submit_oco_group([a, b], group_id="grp_1")

    def test_empty_members_rejected(self) -> None:
        mgr = _make_manager([])
        with pytest.raises(ValueError, match="at least 1 member"):
            mgr.submit_oco_group([], group_id="grp_1")


class TestCancelByGroup:
    def test_cancels_all_members_of_group(self) -> None:
        mgr = _make_manager([])
        a = _pending_entry(member_id="limit_pullback")
        b = _pending_entry(
            member_id="stop_breakout", entry_low=4520.0, entry_high=4530.0
        )
        mgr.submit_oco_group([a, b], group_id="grp_1")

        cancelled = mgr.cancel_by_group("grp_1", reason="test_cancel")
        assert cancelled == 2
        assert "grp_1" not in mgr._groups
        assert mgr._pending == {}

    def test_exclude_key_keeps_one_member(self) -> None:
        mgr = _make_manager([])
        a = _pending_entry(member_id="limit_pullback")
        b = _pending_entry(
            member_id="stop_breakout", entry_low=4520.0, entry_high=4530.0
        )
        mgr.submit_oco_group([a, b], group_id="grp_1")

        # 排除 limit_pullback → 只撤 stop_breakout
        cancelled = mgr.cancel_by_group(
            "grp_1", reason="oco_sibling_filled", exclude_key="sig_1#limit_pullback"
        )
        assert cancelled == 1
        assert "sig_1#limit_pullback" in mgr._pending
        assert "sig_1#stop_breakout" not in mgr._pending

    def test_unknown_group_returns_zero(self) -> None:
        mgr = _make_manager([])
        cancelled = mgr.cancel_by_group("ghost_group")
        assert cancelled == 0


class TestCancelBySymbolPropagatesGroup:
    def test_oco_group_fully_cancelled_via_symbol(self) -> None:
        """反向信号撤同 symbol 时，OCO group 整组撤（任一 member 命中扩展全组）。"""
        mgr = _make_manager([])
        a = _pending_entry(member_id="limit_pullback")
        b = _pending_entry(
            member_id="stop_breakout", entry_low=4520.0, entry_high=4530.0
        )
        mgr.submit_oco_group([a, b], group_id="grp_1")

        cancelled = mgr.cancel_by_symbol("XAUUSD", reason="reverse_signal")
        assert cancelled == 2
        assert mgr._pending == {}
        assert "grp_1" not in mgr._groups


class TestSingleMemberBackwardCompat:
    """单 member 路径必须与原 submit() 行为兼容。"""

    def test_single_member_uses_signal_id_as_key(self) -> None:
        mgr = _make_manager([])
        entry = _pending_entry(member_id="market", group_role="market")
        # OCO group 单 member 也走 submit_oco_group
        mgr.submit_oco_group([entry], group_id="solo_grp")
        # entry_key 是 "sig_1#market"
        assert "sig_1#market" in mgr._pending

    def test_legacy_submit_no_group_id_still_works(self) -> None:
        """旧调用 submit(entry) 不带 group_id → entry_key=signal_id（向后兼容）。"""
        mgr = _make_manager([])
        event = _signal_event()
        entry = PendingEntry(
            signal_event=event,
            trade_params=_trade_params(),
            cost_metrics={},
            entry_low=4498.0,
            entry_high=4502.0,
            reference_price=4500.0,
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        )
        mgr.submit(entry)
        # entry_key 默认 = signal_id
        assert "sig_1" in mgr._pending
        assert mgr._pending["sig_1"].entry_key == "sig_1"
