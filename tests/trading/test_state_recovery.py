from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.trading.state import TradingStateRecovery
from src.trading.state import TradingStateRecoveryPolicy


class DummyStateStore:
    def __init__(self):
        self.warm_started = False
        self.trade_control_state = None
        self.pending_rows = []
        self.events = []

    def warm_start(self) -> None:
        self.warm_started = True

    def load_trade_control_state(self):
        return self.trade_control_state

    def list_active_pending_orders(self):
        return list(self.pending_rows)

    def mark_pending_order_filled(self, info, *, state=None):
        self.events.append(("filled", info["ticket"], dict(state or {})))

    def mark_pending_order_missing(self, info, *, reason):
        self.events.append(("missing", info["ticket"], reason))

    def mark_pending_order_expired(self, info, *, reason):
        self.events.append(("expired", info["ticket"], reason))

    def mark_pending_order_orphan(self, order_row):
        self.events.append(("orphan", getattr(order_row, "ticket", None)))

    def mark_pending_order_cancelled(self, info, *, reason):
        self.events.append(("cancelled", info["ticket"], reason))


class DummyPendingEntryManager:
    def __init__(self, inspect_result=None):
        self.restored = []
        self.inspect_result = inspect_result or {
            "status": "missing",
            "reason": "not_found",
        }

    def restore_mt5_order(self, info):
        self.restored.append(dict(info))

    def inspect_mt5_order(self, info):
        return dict(self.inspect_result)


class DummyTradingModule:
    def __init__(self, orders=None):
        self.orders = list(orders or [])
        self.applied_control = None
        self.cancelled = []

    def apply_trade_control_state(self, state):
        self.applied_control = dict(state)
        return self.applied_control

    def get_orders(self):
        return list(self.orders)

    def cancel_orders_by_tickets(self, tickets):
        self.cancelled.extend(list(tickets))
        return {"canceled": list(tickets), "failed": []}


def test_trading_state_recovery_restores_trade_control() -> None:
    store = DummyStateStore()
    store.trade_control_state = {
        "auto_entry_enabled": False,
        "close_only_mode": True,
        "updated_at": datetime(2026, 4, 2, 9, 0, tzinfo=timezone.utc),
        "reason": "manual_pause",
    }
    recovery = TradingStateRecovery(store)
    trading = DummyTradingModule()

    result = recovery.restore_trade_control(trading)

    assert result == {"restored": True}
    assert trading.applied_control["close_only_mode"] is True


def test_trading_state_recovery_restores_live_pending_orders_and_marks_orphans() -> (
    None
):
    store = DummyStateStore()
    store.pending_rows = [
        {
            "order_ticket": 1001,
            "signal_id": "sig-1",
            "symbol": "XAUUSD",
            "direction": "buy",
            "strategy": "trend_vote",
            "timeframe": "M15",
            "comment": "M15:trend_vote:limit",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
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
    ]
    trading = DummyTradingModule(
        orders=[
            SimpleNamespace(
                ticket=1001, symbol="XAUUSD", type=2, comment="M15:trend_vote:limit"
            ),
            SimpleNamespace(ticket=9999, symbol="XAUUSD", type=2, comment="manual"),
        ]
    )
    pending_manager = DummyPendingEntryManager()
    recovery = TradingStateRecovery(store)

    result = recovery.restore_pending_orders(
        pending_entry_manager=pending_manager,
        trading_module=trading,
    )

    assert result["restored"] == 1
    assert result["orphan"] == 1
    assert pending_manager.restored[0]["ticket"] == 1001
    assert ("orphan", 9999) in store.events


def test_trading_state_recovery_marks_missing_orders_when_not_found() -> None:
    store = DummyStateStore()
    store.pending_rows = [
        {
            "order_ticket": 1002,
            "signal_id": "sig-2",
            "symbol": "XAUUSD",
            "direction": "sell",
            "strategy": "trend_vote",
            "timeframe": "M15",
            "comment": "M15:trend_vote:limit",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "metadata": {},
        }
    ]
    trading = DummyTradingModule(orders=[])
    pending_manager = DummyPendingEntryManager(
        inspect_result={"status": "missing", "reason": "startup_missing"}
    )
    recovery = TradingStateRecovery(store)

    result = recovery.restore_pending_orders(
        pending_entry_manager=pending_manager,
        trading_module=trading,
    )

    assert result["missing"] == 1
    assert ("missing", 1002, "startup_missing") in store.events


def test_trading_state_recovery_uses_policy_for_orphan_and_missing_actions() -> None:
    store = DummyStateStore()
    store.pending_rows = [
        {
            "order_ticket": 1003,
            "signal_id": "sig-3",
            "symbol": "XAUUSD",
            "direction": "sell",
            "strategy": "trend_vote",
            "timeframe": "M15",
            "comment": "M15:trend_vote:limit",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "metadata": {},
        }
    ]
    trading = DummyTradingModule(
        orders=[SimpleNamespace(ticket=9998, symbol="XAUUSD", type=2, comment="manual")]
    )
    pending_manager = DummyPendingEntryManager(
        inspect_result={"status": "missing", "reason": "startup_missing"}
    )
    policy = TradingStateRecoveryPolicy(
        store,
        orphan_action="cancel",
        missing_action="ignore",
    )
    recovery = TradingStateRecovery(store, policy=policy)

    result = recovery.restore_pending_orders(
        pending_entry_manager=pending_manager,
        trading_module=trading,
    )

    assert result["ignored_missing"] == 1
    assert result["orphan_cancelled"] == 1
    assert ("missing", 1003, "startup_missing") not in store.events
    assert ("orphan", 9998) in store.events


# ── P2 回归：cancel_orders_by_tickets standard failed schema ─────────────────


class _DummyTradingModuleStandardFailedSchema:
    """复现仓库标准 failed payload 形状（mt5_trading.py 多处证实）：
    failed=[{'ticket': ..., 'error': ...}] 而非旧的 list[int]。
    """

    def __init__(self, *, fail_tickets: list[int]):
        self._fail_tickets = set(fail_tickets)
        self.cancelled = []

    def apply_trade_control_state(self, state):
        return dict(state)

    def get_orders(self):
        return []

    def cancel_orders_by_tickets(self, tickets):
        self.cancelled.extend(list(tickets))
        return {
            "canceled": [t for t in tickets if t not in self._fail_tickets],
            "failed": [
                {"ticket": t, "error": "market_closed"}
                for t in tickets
                if t in self._fail_tickets
            ],
        }


def test_expired_pending_with_standard_failed_payload_marks_restored_correctly() -> (
    None
):
    """P2 回归：当 broker 返回标准 failed=[{ticket,error}] schema：

    - 旧 _ticket_was_cancelled 对 dict 调 int(item) → TypeError → 外层 except
      吞掉 → cancelled=False → 走 restore_mt5_order → restored += 1（错把
      撤单失败的过期挂单当成"已恢复"）

    修复后：cancelled=False 仍走 restore（合理，撤单失败时让 PendingEntryManager
    继续监控），但 helper 不再因 TypeError 崩；若 ticket 真在 failed 列表里，
    helper 仍能识别 → cancelled=False（与旧行为一致），但路径稳定不抛。
    """
    store = DummyStateStore()
    store.pending_rows = [
        {
            "order_ticket": 5001,
            "signal_id": "sig-x",
            "symbol": "XAUUSD",
            "direction": "buy",
            "strategy": "trend_vote",
            "timeframe": "M15",
            "comment": "M15:trend_vote:limit",
            # 已过期
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=5),
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
    ]
    trading = _DummyTradingModuleStandardFailedSchema(fail_tickets=[5001])
    # broker 仍报告订单存在（live_order）→ 进入 expires_at 分支
    trading.get_orders = lambda: [  # type: ignore[method-assign]
        SimpleNamespace(
            ticket=5001, symbol="XAUUSD", type=2, comment="M15:trend_vote:limit"
        )
    ]
    pending_manager = DummyPendingEntryManager()

    recovery = TradingStateRecovery(store)
    result = recovery.restore_pending_orders(
        pending_entry_manager=pending_manager,
        trading_module=trading,
    )

    # 标准 failed 形状下，broker 返回 failed=[{ticket: 5001}]：
    # - 修复前 helper TypeError → cancelled=False → restored=1，expired=0
    #   （但 broker 真的报 fail，所以 cancelled=False 是合理结论；问题在路径不稳）
    # - 修复后 helper 正确识别 ticket 在 failed → cancelled=False → restored=1
    # 关键断言：result 必须是稳定 dict（无 key 缺失），即 helper 不再抛
    assert isinstance(result, dict)
    # ticket 5001 由 fail_tickets 配置 → cancelled=False → 进 restore 分支
    assert result["restored"] == 1
    assert result["expired"] == 0


def test_expired_pending_correctly_marked_when_cancel_succeeds_standard_schema() -> (
    None
):
    """与上一 test 对比：fail_tickets=[]（broker 全成功）→ cancelled=True →
    expired += 1。验证 helper 在 standard schema 下成功路径也正确识别。
    """
    store = DummyStateStore()
    store.pending_rows = [
        {
            "order_ticket": 5002,
            "signal_id": "sig-y",
            "symbol": "XAUUSD",
            "direction": "buy",
            "strategy": "trend_vote",
            "timeframe": "M15",
            "comment": "M15:trend_vote:limit",
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=5),
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
    ]
    trading = _DummyTradingModuleStandardFailedSchema(fail_tickets=[])
    trading.get_orders = lambda: [  # type: ignore[method-assign]
        SimpleNamespace(
            ticket=5002, symbol="XAUUSD", type=2, comment="M15:trend_vote:limit"
        )
    ]
    pending_manager = DummyPendingEntryManager()

    recovery = TradingStateRecovery(store)
    result = recovery.restore_pending_orders(
        pending_entry_manager=pending_manager,
        trading_module=trading,
    )
    assert result["expired"] == 1
    assert result["restored"] == 0


def test_orphan_with_standard_failed_payload_does_not_crash_startup() -> None:
    """P2 回归（recovery_policy）：旧 helper 同源 TypeError，且 orphan 路径
    没有 try/except 兜底 → handle_orphan_pending_order 直接抛 → 整个
    restore_pending_orders 中断 → startup recovery 挂掉。
    """
    store = DummyStateStore()
    # store.pending_rows 空，让 orphan 路径触发（broker 返回不在 store 里的 ticket）
    store.pending_rows = []
    trading = _DummyTradingModuleStandardFailedSchema(fail_tickets=[7777])
    trading.get_orders = lambda: [  # type: ignore[method-assign]
        SimpleNamespace(ticket=7777, symbol="XAUUSD", type=2, comment="orphan_manual")
    ]
    pending_manager = DummyPendingEntryManager()

    policy = TradingStateRecoveryPolicy(store, orphan_action="cancel")
    recovery = TradingStateRecovery(store, policy=policy)

    # 必须不抛
    result = recovery.restore_pending_orders(
        pending_entry_manager=pending_manager,
        trading_module=trading,
    )
    assert isinstance(result, dict)
    # ticket 在 failed 里 → cancelled=False → 仍标 orphan（非 orphan_cancelled）
    assert result["orphan"] == 1
    assert result["orphan_cancelled"] == 0
