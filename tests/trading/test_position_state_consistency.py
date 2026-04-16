"""Tests for position state consistency improvements.

Covers: dirty flush, initial_stop_loss recovery validation,
resolver conflict detection, reconcile step isolation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.trading.closeout import ExposureCloseoutController, ExposureCloseoutService
from src.trading.execution.sizing import TradeParameters
from src.trading.positions import PositionManager
from src.trading.positions.reconciliation import (
    _resolve_position_context,
    sync_open_positions,
)


class DummyTrading:
    def __init__(self, positions=None):
        self._positions = list(positions or [])
        self.modify_calls = []

    def get_positions(self, symbol=None):
        if symbol:
            return [p for p in self._positions if getattr(p, "symbol", None) == symbol]
        return list(self._positions)

    def get_position_close_details(self, ticket, *, symbol=None, lookback_days=7):
        return None

    def modify_positions(self, **kwargs):
        self.modify_calls.append(kwargs)
        return {"modified": [kwargs.get("ticket")], "failed": []}

    def account_info(self):
        return None


def _make_manager(positions=None) -> PositionManager:
    trading = DummyTrading(positions)
    closeout_svc = ExposureCloseoutService.__new__(ExposureCloseoutService)
    closeout = ExposureCloseoutController(closeout_svc)
    return PositionManager(trading, closeout)


def _make_params(**kw) -> TradeParameters:
    defaults = {
        "entry_price": 2000.0,
        "stop_loss": 1980.0,
        "take_profit": 2060.0,
        "position_size": 0.01,
        "risk_reward_ratio": 3.0,
        "atr_value": 15.0,
        "sl_distance": 20.0,
        "tp_distance": 60.0,
    }
    defaults.update(kw)
    return TradeParameters(**defaults)


# ── Dirty flush tests ──


def test_update_price_marks_dirty_on_peak_change():
    """peak_price 变化时应标记脏位。"""
    mgr = _make_manager()
    pos = mgr.track_position(
        ticket=1001,
        signal_id="sig1",
        symbol="XAUUSD",
        action="buy",
        params=_make_params(),
    )
    assert not pos._dirty
    assert pos._version == 0

    # 价格上涨 → 新 peak
    mgr.update_price(1001, 2010.0)
    assert pos._dirty
    assert pos._version == 1
    assert pos.highest_price == 2010.0


def test_update_price_no_dirty_on_lower_price():
    """价格下跌不应标记脏位（peak 未变）。"""
    mgr = _make_manager()
    pos = mgr.track_position(
        ticket=1002,
        signal_id="sig2",
        symbol="XAUUSD",
        action="buy",
        params=_make_params(),
    )
    mgr.update_price(1002, 2010.0)  # new peak
    pos._dirty = False
    pos._flushed_version = pos._version

    mgr.update_price(1002, 2005.0)  # lower than peak
    assert not pos._dirty


def test_flush_dirty_positions_clears_dirty():
    """_flush_dirty_positions 应清除脏标记并调用 on_position_updated。"""
    mgr = _make_manager()
    update_calls = []
    mgr.set_state_hooks(
        on_position_updated=lambda pos, reason: update_calls.append(
            (pos.ticket, reason)
        )
    )

    pos = mgr.track_position(
        ticket=1003,
        signal_id="sig3",
        symbol="XAUUSD",
        action="buy",
        params=_make_params(),
    )
    mgr.update_price(1003, 2020.0)
    assert pos._dirty

    mgr._flush_dirty_positions()
    assert not pos._dirty
    assert pos._flushed_version == pos._version
    # update_price 可能也触发了 chandelier trail 回调，dirty_flush 应在最后
    flush_calls = [(t, r) for t, r in update_calls if r == "dirty_flush"]
    assert len(flush_calls) == 1
    assert flush_calls[0] == (1003, "dirty_flush")


def test_flush_skips_non_dirty():
    """非脏持仓不应触发 flush。"""
    mgr = _make_manager()
    update_calls = []
    mgr.set_state_hooks(
        on_position_updated=lambda pos, reason: update_calls.append(reason)
    )

    mgr.track_position(
        ticket=1004,
        signal_id="sig4",
        symbol="XAUUSD",
        action="buy",
        params=_make_params(),
    )
    mgr._flush_dirty_positions()
    assert len(update_calls) == 0


def test_status_exposes_dirty_count():
    """status() 应暴露 dirty_positions 计数。"""
    mgr = _make_manager()
    mgr.track_position(
        ticket=1005,
        signal_id="sig5",
        symbol="XAUUSD",
        action="buy",
        params=_make_params(),
    )
    assert mgr.status()["dirty_positions"] == 0
    mgr.update_price(1005, 2050.0)
    assert mgr.status()["dirty_positions"] == 1


# ── Reconcile step isolation tests ──


def test_reconcile_loop_step_isolation():
    """某个 reconcile 步骤失败不应阻塞后续步骤。

    直接调用步骤序列逻辑来验证，避免线程/事件的复杂性。
    """
    mgr = _make_manager()

    call_order: list[str] = []

    def failing_flush():
        call_order.append("flush")
        raise RuntimeError("flush error")

    def ok_mt5():
        call_order.append("mt5")

    def ok_regime():
        call_order.append("regime")

    mgr._flush_dirty_positions = failing_flush
    mgr._reconcile_with_mt5 = ok_mt5
    mgr._check_regime_changes = ok_regime
    mgr._run_end_of_day_closeout = lambda: call_order.append("eod")
    mgr._run_margin_guard = lambda: call_order.append("margin")

    # 模拟 _reconcile_loop 的一次迭代（不启动线程）
    step_errors: list[str] = []
    for step_name, step_fn in (
        ("flush_dirty", mgr._flush_dirty_positions),
        ("end_of_day", mgr._run_end_of_day_closeout),
        ("reconcile_mt5", mgr._reconcile_with_mt5),
        ("regime_changes", mgr._check_regime_changes),
        ("margin_guard", mgr._run_margin_guard),
    ):
        try:
            step_fn()
        except Exception as exc:
            step_errors.append(f"{step_name}: {exc}")

    # flush 失败了，但 eod/mt5/regime/margin 仍然执行
    assert "flush" in call_order
    assert "eod" in call_order
    assert "mt5" in call_order
    assert "regime" in call_order
    assert "margin" in call_order
    assert len(step_errors) == 1
    assert "flush" in step_errors[0]


# ── Resolver conflict detection tests ──


def test_resolve_position_context_db_priority():
    """DB 值应覆盖 comment 解码值。"""
    mgr = _make_manager()
    mgr._position_state_resolver = lambda t: {
        "entry_price": 2000.0,
        "strategy": "trend",
    }
    mgr._position_context_resolver = lambda t, c: {
        "entry_price": 1999.0,
        "timeframe": "H1",
    }

    ctx = _resolve_position_context(mgr, 999, "test_comment")
    assert ctx["entry_price"] == 2000.0  # DB wins
    assert ctx["strategy"] == "trend"  # from DB
    assert ctx["timeframe"] == "H1"  # from comment (DB didn't have it)


def test_resolve_position_context_comment_fills_gaps():
    """DB 缺失的字段应由 comment 填补。"""
    mgr = _make_manager()
    mgr._position_state_resolver = lambda t: {"signal_id": "s1"}
    mgr._position_context_resolver = lambda t, c: {
        "signal_id": "s2",
        "regime": "trending",
    }

    ctx = _resolve_position_context(mgr, 888, "")
    assert ctx["signal_id"] == "s1"  # DB wins
    assert ctx["regime"] == "trending"  # comment fills gap


def test_resolve_position_context_handles_resolver_failure():
    """resolver 异常应返回空而不崩溃。"""
    mgr = _make_manager()
    mgr._position_state_resolver = lambda t: (_ for _ in ()).throw(
        RuntimeError("db error")
    )
    mgr._position_context_resolver = None

    ctx = _resolve_position_context(mgr, 777, "")
    assert ctx == {}


# ── initial_stop_loss recovery validation tests ──


def test_initial_sl_recovery_atr_estimate_on_suspiciously_small():
    """initial_risk < 0.1 ATR 且 DB 无记录时，应用 ATR 倍数估算。"""
    mgr = _make_manager(
        positions=[
            SimpleNamespace(
                ticket=5001,
                symbol="XAUUSD",
                type=0,
                price_open=2000.0,
                sl=1999.5,
                tp=2060.0,
                volume=0.01,
                comment="test",
                time=datetime.now(timezone.utc),
            )
        ]
    )
    mgr._position_state_resolver = lambda t: {
        "signal_id": "s5001",
        "atr_at_entry": 15.0,
        # 注意：没有 initial_stop_loss → fallback 到 SL=1999.5
        # initial_risk = |2000 - 1999.5| = 0.5 < 0.1 * 15.0 = 1.5 → 触发估算
    }
    mgr._position_context_resolver = None

    result = sync_open_positions(mgr)
    assert result["synced"] == 1

    with mgr._lock:
        pos = mgr._positions[5001]
    # 应该用 ATR 估算而非 0.5
    assert pos.initial_risk > 1.0
    assert pos.initial_risk == 15.0 * 1.5  # max(sl_atr_mult=0, 1.5) * ATR


# ── Exit code classification tests ──


def test_classify_exit_code():
    from src.entrypoint.supervisor import _classify_exit_code

    assert _classify_exit_code(0) == "normal"
    assert _classify_exit_code(1) == "transient"
    assert _classify_exit_code(127) == "transient"
    assert _classify_exit_code(128) == "fatal"
    assert _classify_exit_code(137) == "fatal"  # SIGKILL
    assert _classify_exit_code(139) == "fatal"  # SIGSEGV
    assert _classify_exit_code(-1) == "fatal"  # Windows
    assert _classify_exit_code(-1073741819) == "fatal"  # ACCESS_VIOLATION
    assert _classify_exit_code(None) == "unknown"
