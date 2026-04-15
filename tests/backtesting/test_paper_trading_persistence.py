"""F-4 Paper Trading 持久化改动的单元级回归测试。

覆盖：
- F-4.1 tracker.start() 立即触发一次 flush（避免 30s 首次窗口丢失）
- F-4.2 open trade 通过 provider 定期 upsert（MFE/MAE 运行时同步到 DB）
- F-4.3 bridge resume：recover_fn 返回 payload → session_id 复用 + open trades 恢复
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from src.backtesting.paper_trading.bridge import PaperTradingBridge
from src.backtesting.paper_trading.config import PaperTradingConfig
from src.backtesting.paper_trading.models import PaperTradeRecord
from src.backtesting.paper_trading.tracker import PaperTradeTracker


# ── 测试辅助 ────────────────────────────────────────────────────────────

class _DummyRepo:
    def __init__(self) -> None:
        self.upserted_sessions: List[Dict[str, Any]] = []
        self.written_trades: List[Any] = []

    def upsert_session(self, session: Dict[str, Any]) -> None:
        self.upserted_sessions.append(session)

    def write_trades(self, trades: List[PaperTradeRecord]) -> None:
        self.written_trades.extend(trades)


class _DummyDB:
    def __init__(self) -> None:
        self.paper_trading_repo = _DummyRepo()


def _dummy_quote_fn(symbol: str) -> Any:
    return SimpleNamespace(bid=3000.0, ask=3001.0)


def _make_record(trade_id: str = "t-1", exit_time=None) -> PaperTradeRecord:
    return PaperTradeRecord(
        trade_id=trade_id,
        session_id="ps_test",
        strategy="structured_trend_continuation",
        direction="buy",
        symbol="XAUUSD",
        timeframe="M30",
        entry_time=datetime.now(timezone.utc),
        entry_price=3000.0,
        stop_loss=2990.0,
        take_profit=3020.0,
        position_size=0.1,
        confidence=0.8,
        regime="trend",
        exit_time=exit_time,
    )


# ── F-4.1 立即 flush ────────────────────────────────────────────────────

def test_tracker_start_triggers_immediate_flush() -> None:
    """F-4.1: tracker.start() 后立即触发 _flush_now，不等 30s flush_interval。"""
    db = _DummyDB()
    tracker = PaperTradeTracker(db_writer=db, flush_interval=30.0)

    # 预先注入一笔 pending session（模拟 bridge.start() 已创建 session）
    from src.backtesting.paper_trading.models import PaperSession
    session = PaperSession(
        session_id="ps_abc",
        started_at=datetime.now(timezone.utc),
        initial_balance=2000.0,
    )
    tracker.save_session(session)

    # start 应该立即 flush pending session 入库（不必等 30s）
    tracker.start()
    try:
        assert len(db.paper_trading_repo.upserted_sessions) == 1
        assert db.paper_trading_repo.upserted_sessions[0]["session_id"] == "ps_abc"
    finally:
        tracker.stop()


# ── F-4.2 open trade 快照 provider ─────────────────────────────────────

def test_tracker_pulls_open_trades_via_provider_on_flush() -> None:
    """F-4.2: flush 时主动从 provider 拉取 open trades，upsert 到 DB。"""
    db = _DummyDB()
    tracker = PaperTradeTracker(db_writer=db, flush_interval=30.0)

    open_trades = [_make_record("open-1"), _make_record("open-2")]
    tracker.set_open_trades_snapshot_provider(lambda: list(open_trades))

    tracker._flush_now()
    assert len(db.paper_trading_repo.written_trades) == 2
    assert {t.trade_id for t in db.paper_trading_repo.written_trades} == {"open-1", "open-2"}


def test_tracker_snapshot_provider_handles_empty_list() -> None:
    """provider 返回空列表不应触发 DB 写入。"""
    db = _DummyDB()
    tracker = PaperTradeTracker(db_writer=db, flush_interval=30.0)
    tracker.set_open_trades_snapshot_provider(lambda: [])
    tracker._flush_now()
    assert db.paper_trading_repo.written_trades == []


def test_tracker_snapshot_provider_exception_does_not_break_flush() -> None:
    """provider 抛异常时 flush 仍应继续（处理 closed trades 等其他数据）。"""
    db = _DummyDB()
    tracker = PaperTradeTracker(db_writer=db, flush_interval=30.0)

    def _bad_provider():
        raise RuntimeError("provider failed")

    tracker.set_open_trades_snapshot_provider(_bad_provider)
    tracker.on_trade_closed(_make_record("close-1"))
    tracker._flush_now()
    # 即使 provider 异常，pending_trades 里的 closed trade 仍然被 flush 到 DB
    assert any(t.trade_id == "close-1" for t in db.paper_trading_repo.written_trades)


def test_bridge_snapshot_open_trades_returns_independent_copies() -> None:
    """bridge.snapshot_open_trades 返回副本，确保 portfolio 线程后续修改不影响快照。"""
    config = PaperTradingConfig(enabled=True, initial_balance=2000.0, max_positions=3)
    bridge = PaperTradingBridge(config=config, market_quote_fn=_dummy_quote_fn)
    bridge.start()
    try:
        # 手动插入一个 open position 到 portfolio
        record = _make_record("snap-1")
        bridge._portfolio._open_positions[record.trade_id] = record

        snapshot = bridge.snapshot_open_trades()
        assert len(snapshot) == 1
        assert snapshot[0].trade_id == "snap-1"

        # 修改副本不影响 portfolio 内的原始对象
        snapshot[0].current_sl = 9999.0
        assert bridge._portfolio._open_positions["snap-1"].current_sl != 9999.0
    finally:
        bridge.stop()


# ── F-4.3 resume recovery ─────────────────────────────────────────────

def test_bridge_attempt_resume_fresh_start_when_flag_disabled() -> None:
    """config.resume_active_session=False 时即使 recover_fn 返回 payload 也不 resume。"""
    config = PaperTradingConfig(
        enabled=True,
        initial_balance=2000.0,
        resume_active_session=False,
    )
    recover_called = []

    def _recover():
        recover_called.append(True)
        return {"session": {"session_id": "old-session"}, "open_trades": []}

    bridge = PaperTradingBridge(
        config=config, market_quote_fn=_dummy_quote_fn, recover_fn=_recover
    )
    bridge.start()
    try:
        # resume flag 关闭 → 新 session_id（非 "old-session"），recover_fn 未被调用
        assert bridge.session is not None
        assert bridge.session.session_id != "old-session"
        assert bridge.session.session_id.startswith("ps_")
        assert recover_called == []
    finally:
        bridge.stop()


def test_bridge_attempt_resume_reuses_session_and_open_trades() -> None:
    """config.resume_active_session=True 且 recover_fn 返回 payload 时，复用 session_id 并恢复 open trades。"""
    config = PaperTradingConfig(
        enabled=True,
        initial_balance=2000.0,
        resume_active_session=True,
    )
    previous_session_id = "ps_previous"
    open_record = _make_record("resume-trade-1")
    open_record.session_id = previous_session_id

    def _recover():
        return {
            "session": {
                "session_id": previous_session_id,
                "started_at": datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc),
                "initial_balance": 2000.0,
                "total_pnl": 50.0,
                "total_trades": 3,
                "winning_trades": 2,
                "losing_trades": 1,
                "max_drawdown_pct": 0.5,
            },
            "open_trades": [open_record],
        }

    bridge = PaperTradingBridge(
        config=config, market_quote_fn=_dummy_quote_fn, recover_fn=_recover
    )
    bridge.start()
    try:
        assert bridge.session is not None
        assert bridge.session.session_id == previous_session_id
        assert bridge.session.total_pnl == 50.0
        assert bridge.session.total_trades == 3
        # open trades 已恢复到 portfolio
        assert bridge._portfolio is not None
        assert "resume-trade-1" in bridge._portfolio._open_positions
        # balance = initial_balance + total_pnl
        assert bridge._portfolio.current_balance == 2050.0
        # active_symbols 已同步
        assert "XAUUSD" in bridge._active_symbols
    finally:
        bridge.stop()


def test_bridge_attempt_resume_fresh_start_when_recover_returns_none() -> None:
    """resume_active_session=True 但 recover_fn 返回 None（DB 里没未完结 session）时 fresh start。"""
    config = PaperTradingConfig(
        enabled=True,
        initial_balance=2000.0,
        resume_active_session=True,
    )
    bridge = PaperTradingBridge(
        config=config, market_quote_fn=_dummy_quote_fn, recover_fn=lambda: None
    )
    bridge.start()
    try:
        assert bridge.session is not None
        assert bridge.session.session_id.startswith("ps_")
        assert bridge._portfolio.open_position_count == 0
    finally:
        bridge.stop()


def test_bridge_attempt_resume_fresh_start_when_recover_raises() -> None:
    """recover_fn 异常时 fresh start 并记录 warning（bridge 不能因此崩）。"""
    config = PaperTradingConfig(
        enabled=True,
        initial_balance=2000.0,
        resume_active_session=True,
    )

    def _bad():
        raise RuntimeError("DB down")

    bridge = PaperTradingBridge(
        config=config, market_quote_fn=_dummy_quote_fn, recover_fn=_bad
    )
    bridge.start()
    try:
        assert bridge.session is not None
        assert bridge.session.session_id.startswith("ps_")
    finally:
        bridge.stop()
