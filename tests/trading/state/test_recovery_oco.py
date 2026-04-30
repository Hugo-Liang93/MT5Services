"""ADR-013 P4-residual #2: 重启 recovery 重建 OCO `_groups` 反向索引。

覆盖：
- 单 group + 双 placed members → restore_group_index 重建索引
- 单 group 中 sibling 已 filled，本 placed 仍存在 → 立即撤本 placed（一致性校验）
- 多 group 混合
- 无 group 信息（旧数据）→ 不调 restore_group_index
- group 中 sibling cancel broker 失败 → 仍标本地 cancelled（避免重复 fill）

`_groups` 反向索引重建是 OCO any-fill 撤 sibling 的前提：进程重启后
`_pending` 字典清空，broker 侧仍有挂单，必须确保 `_groups` 与
`_mt5_orders` 同步重建。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

from src.trading.state import TradingStateRecovery


class _OcoStateStore:
    """支持 OCO group 查询的 dummy store。"""

    def __init__(self) -> None:
        self.warm_started = False
        self.trade_control_state: Any = None
        # 当前 placed rows（list_active_pending_orders 返回）
        self.pending_rows: List[Dict[str, Any]] = []
        # 全状态 rows（list_pending_orders_by_groups 返回；按 group_id 索引）
        self.group_rows: Dict[str, List[Dict[str, Any]]] = {}
        self.events: List[tuple] = []

    def warm_start(self) -> None:
        self.warm_started = True

    def load_trade_control_state(self) -> Any:
        return self.trade_control_state

    def list_active_pending_orders(self) -> List[Dict[str, Any]]:
        return list(self.pending_rows)

    def list_pending_orders_by_groups(
        self, group_ids: List[str]
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for gid in group_ids:
            out.extend(self.group_rows.get(gid, []))
        return out

    def mark_pending_order_filled(
        self, info: Dict[str, Any], *, state: Any = None
    ) -> None:
        self.events.append(("filled", info["ticket"], dict(state or {})))

    def mark_pending_order_missing(self, info: Dict[str, Any], *, reason: str) -> None:
        self.events.append(("missing", info["ticket"], reason))

    def mark_pending_order_expired(self, info: Dict[str, Any], *, reason: str) -> None:
        self.events.append(("expired", info["ticket"], reason))

    def mark_pending_order_orphan(self, order_row: Any) -> None:
        self.events.append(("orphan", getattr(order_row, "ticket", None)))

    def mark_pending_order_cancelled(
        self, info: Dict[str, Any], *, reason: str
    ) -> None:
        self.events.append(("cancelled", info["ticket"], reason))


class _OcoPendingEntryManager:
    def __init__(self) -> None:
        self.restored: List[Dict[str, Any]] = []
        self.group_index_calls: List[tuple[str, set[str]]] = []
        self.inspect_result = {"status": "missing", "reason": "not_found"}

    def restore_mt5_order(self, info: Dict[str, Any]) -> None:
        self.restored.append(dict(info))

    def restore_group_index(self, group_id: str, entry_keys: Any) -> None:
        keys_set = {str(k) for k in entry_keys if k}
        self.group_index_calls.append((group_id, keys_set))

    def inspect_mt5_order(self, info: Dict[str, Any]) -> Dict[str, Any]:
        return dict(self.inspect_result)


class _OcoTradingModule:
    def __init__(self, *, orders: List[Any] | None = None) -> None:
        self.orders = list(orders or [])
        self.cancelled: List[int] = []
        self.cancel_fail_tickets: set[int] = set()

    def get_orders(self) -> List[Any]:
        return list(self.orders)

    def apply_trade_control_state(self, state: Any) -> Any:
        return dict(state)

    def cancel_orders_by_tickets(self, tickets: List[int]) -> Dict[str, Any]:
        self.cancelled.extend(list(tickets))
        canceled = [t for t in tickets if t not in self.cancel_fail_tickets]
        failed = [
            {"ticket": t, "error": "broker_reject"}
            for t in tickets
            if t in self.cancel_fail_tickets
        ]
        return {"canceled": canceled, "failed": failed}


def _make_pending_row(
    *,
    ticket: int,
    signal_id: str,
    group_id: str,
    member_id: str,
    role: str,
    status: str = "placed",
) -> Dict[str, Any]:
    return {
        "order_ticket": ticket,
        "signal_id": signal_id,
        "symbol": "XAUUSD",
        "direction": "buy",
        "strategy": "structured_price_action",
        "timeframe": "M15",
        "comment": "M15:price_action:limit",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        "status": status,
        "order_group_id": group_id,
        "group_member_id": member_id,
        "group_role": role,
        "metadata": {
            "params": {
                "entry_price": 3000.0,
                "stop_loss": 2990.0,
                "take_profit": 3020.0,
                "position_size": 0.1,
                "atr_value": 5.0,
            }
        },
    }


# ── 重建反向索引 ────────────────────────────────────────────────────────────


def test_restore_pending_orders_rebuilds_groups_index_for_oco() -> None:
    """OCO group 双 placed members → 重建 _groups[group_id] = {entry_keys}。"""
    store = _OcoStateStore()
    g = "group-uuid-1"
    row_limit = _make_pending_row(
        ticket=2001,
        signal_id="sig-1#limit_pullback",
        group_id=g,
        member_id="limit_pullback",
        role="limit",
    )
    row_stop = _make_pending_row(
        ticket=2002,
        signal_id="sig-1#stop_breakout",
        group_id=g,
        member_id="stop_breakout",
        role="stop",
    )
    store.pending_rows = [row_limit, row_stop]
    store.group_rows[g] = [row_limit, row_stop]

    trading = _OcoTradingModule(
        orders=[
            SimpleNamespace(ticket=2001, symbol="XAUUSD", type=2, comment="m1"),
            SimpleNamespace(ticket=2002, symbol="XAUUSD", type=4, comment="m2"),
        ]
    )
    pending_manager = _OcoPendingEntryManager()
    recovery = TradingStateRecovery(store)

    summary = recovery.restore_pending_orders(
        pending_entry_manager=pending_manager,
        trading_module=trading,
    )

    assert summary["restored"] == 2
    assert summary["oco_groups_restored"] == 1
    assert summary["oco_sibling_cancelled"] == 0

    # 两个 entry 都进 _mt5_orders 重建
    assert len(pending_manager.restored) == 2

    # restore_group_index 调用一次，含两 entry_keys
    assert len(pending_manager.group_index_calls) == 1
    gid, keys = pending_manager.group_index_calls[0]
    assert gid == g
    assert keys == {"sig-1#limit_pullback", "sig-1#stop_breakout"}


# ── 一致性校验：sibling 已 filled → 撤剩下 placed ───────────────────────────


def test_sibling_already_filled_triggers_immediate_cancel() -> None:
    """同 group：member1=filled, member2=placed → 立即撤 member2。"""
    store = _OcoStateStore()
    g = "group-uuid-2"
    row_filled = _make_pending_row(
        ticket=3001,
        signal_id="sig-2#limit_pullback",
        group_id=g,
        member_id="limit_pullback",
        role="limit",
        status="filled",
    )
    row_orphan = _make_pending_row(
        ticket=3002,
        signal_id="sig-2#stop_breakout",
        group_id=g,
        member_id="stop_breakout",
        role="stop",
        status="placed",
    )
    # placed 列表只有 orphan；group 全状态含 filled + placed
    store.pending_rows = [row_orphan]
    store.group_rows[g] = [row_filled, row_orphan]

    trading = _OcoTradingModule(
        orders=[SimpleNamespace(ticket=3002, symbol="XAUUSD", type=4, comment="m")]
    )
    pending_manager = _OcoPendingEntryManager()
    recovery = TradingStateRecovery(store)

    summary = recovery.restore_pending_orders(
        pending_entry_manager=pending_manager,
        trading_module=trading,
    )

    # orphan placed 未走 restore，被立即撤
    assert summary["restored"] == 0
    assert summary["oco_sibling_cancelled"] == 1
    assert summary["oco_groups_restored"] == 0
    assert pending_manager.restored == []
    assert pending_manager.group_index_calls == []
    assert 3002 in trading.cancelled
    assert ("cancelled", 3002, "startup_oco_sibling_filled") in store.events


def test_sibling_cancel_broker_reject_still_marks_cancelled_locally() -> None:
    """broker 拒绝撤单时本地仍标 cancelled，避免重启后重复触发 fill 链。"""
    store = _OcoStateStore()
    g = "group-uuid-3"
    row_filled = _make_pending_row(
        ticket=4001,
        signal_id="sig-3#limit",
        group_id=g,
        member_id="limit_pullback",
        role="limit",
        status="filled",
    )
    row_orphan = _make_pending_row(
        ticket=4002,
        signal_id="sig-3#stop",
        group_id=g,
        member_id="stop_breakout",
        role="stop",
        status="placed",
    )
    store.pending_rows = [row_orphan]
    store.group_rows[g] = [row_filled, row_orphan]

    trading = _OcoTradingModule(
        orders=[SimpleNamespace(ticket=4002, symbol="XAUUSD", type=4, comment="m")]
    )
    trading.cancel_fail_tickets = {4002}  # broker 拒绝
    pending_manager = _OcoPendingEntryManager()
    recovery = TradingStateRecovery(store)

    summary = recovery.restore_pending_orders(
        pending_entry_manager=pending_manager,
        trading_module=trading,
    )

    assert summary["oco_sibling_cancelled"] == 1
    # 即使 broker reject，本地仍 mark cancelled
    assert ("cancelled", 4002, "startup_oco_sibling_filled") in store.events


# ── 多 group 混合 ───────────────────────────────────────────────────────────


def test_mixed_groups_restored_and_cancelled_independently() -> None:
    """两个 group：A 双 placed → 重建索引；B 有 filled sibling → 撤 B 的 placed。"""
    store = _OcoStateStore()
    g_a = "group-a"
    g_b = "group-b"
    row_a1 = _make_pending_row(
        ticket=5001,
        signal_id="sigA#limit",
        group_id=g_a,
        member_id="limit",
        role="limit",
    )
    row_a2 = _make_pending_row(
        ticket=5002,
        signal_id="sigA#stop",
        group_id=g_a,
        member_id="stop",
        role="stop",
    )
    row_b_filled = _make_pending_row(
        ticket=5003,
        signal_id="sigB#limit",
        group_id=g_b,
        member_id="limit",
        role="limit",
        status="filled",
    )
    row_b_orphan = _make_pending_row(
        ticket=5004,
        signal_id="sigB#stop",
        group_id=g_b,
        member_id="stop",
        role="stop",
    )
    store.pending_rows = [row_a1, row_a2, row_b_orphan]
    store.group_rows[g_a] = [row_a1, row_a2]
    store.group_rows[g_b] = [row_b_filled, row_b_orphan]

    trading = _OcoTradingModule(
        orders=[
            SimpleNamespace(ticket=5001, symbol="XAUUSD", type=2, comment="a1"),
            SimpleNamespace(ticket=5002, symbol="XAUUSD", type=4, comment="a2"),
            SimpleNamespace(ticket=5004, symbol="XAUUSD", type=4, comment="b"),
        ]
    )
    pending_manager = _OcoPendingEntryManager()
    recovery = TradingStateRecovery(store)

    summary = recovery.restore_pending_orders(
        pending_entry_manager=pending_manager,
        trading_module=trading,
    )

    assert summary["restored"] == 2  # group A 双 member
    assert summary["oco_groups_restored"] == 1  # 仅 group A 重建
    assert summary["oco_sibling_cancelled"] == 1  # group B orphan 撤
    assert 5004 in trading.cancelled
    assert 5001 not in trading.cancelled
    assert 5002 not in trading.cancelled

    # group A 索引重建，含双 entry_keys
    a_calls = [
        (gid, keys) for gid, keys in pending_manager.group_index_calls if gid == g_a
    ]
    assert len(a_calls) == 1
    assert a_calls[0][1] == {"sigA#limit", "sigA#stop"}


# ── 无 group 信息 ───────────────────────────────────────────────────────────


def test_legacy_rows_without_group_skip_restore_group_index() -> None:
    """无 order_group_id 的旧数据（pre-ADR-013）→ 不调 restore_group_index。"""
    store = _OcoStateStore()
    legacy_row = {
        "order_ticket": 6001,
        "signal_id": "legacy-sig",
        "symbol": "XAUUSD",
        "direction": "buy",
        "strategy": "trend_continuation",
        "timeframe": "H1",
        "comment": "H1:trend",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        "status": "placed",
        # 故意不带 order_group_id
        "metadata": {"params": {}},
    }
    store.pending_rows = [legacy_row]
    trading = _OcoTradingModule(
        orders=[SimpleNamespace(ticket=6001, symbol="XAUUSD", type=2, comment="x")]
    )
    pending_manager = _OcoPendingEntryManager()
    recovery = TradingStateRecovery(store)

    summary = recovery.restore_pending_orders(
        pending_entry_manager=pending_manager,
        trading_module=trading,
    )

    assert summary["restored"] == 1
    assert summary["oco_groups_restored"] == 0
    assert pending_manager.group_index_calls == []
