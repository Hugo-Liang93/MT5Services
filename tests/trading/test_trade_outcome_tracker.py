"""TradeOutcomeTracker 单元测试。

覆盖：
- 正常关仓（close_price 存在）→ 盈亏计算 + 回调 + DB 写入
- close_price=None → unresolved 终态记录（不丢弃交易）
- close_source 从 pos.close_source（公开字段）传递
- summary() 包含 unresolved_closes 计数
- 未注册的 signal_id 不报错
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

import pytest

from src.trading.tracking import TradeOutcomeTracker


def _make_pos(
    *,
    signal_id: str = "sig-1",
    symbol: str = "XAUUSD",
    action: str = "buy",
    close_source: Optional[str] = None,
) -> SimpleNamespace:
    # §0y P2：使用 TrackedPosition 实际公开字段 close_source（参
    # src/trading/positions/manager.py:116 + reconciliation.py:376）。旧
    # fixture 写 _close_source 与旧 trade_outcome.py 读 _close_source 对称
    # 错误，掩盖了 close_source 永远落库为默认值的 bug。
    pos = SimpleNamespace(signal_id=signal_id, symbol=symbol, action=action)
    if close_source is not None:
        pos.close_source = close_source
    return pos


class TestNormalClose:
    """close_price 存在时的正常评估路径。"""

    def test_buy_win(self):
        outcomes: List[Tuple] = []
        written: List[Any] = []
        tracker = TradeOutcomeTracker(
            write_fn=lambda rows: written.extend(rows),
            on_outcome_fn=lambda *a, **kw: outcomes.append((a, kw)),
        )
        tracker.on_trade_opened(
            signal_id="sig-1", symbol="XAUUSD", timeframe="M5",
            strategy="sma_trend", direction="buy", fill_price=2000.0, confidence=0.75,
        )
        tracker.on_position_closed(_make_pos(action="buy"), close_price=2010.0)

        summary = tracker.summary()
        assert summary["total_evaluated"] == 1
        assert summary["total_wins"] == 1
        assert summary["unresolved_closes"] == 0
        assert summary["active_trades"] == 0

        # 回调被触发
        assert len(outcomes) == 1
        assert outcomes[0][0][1] is True  # won=True
        assert outcomes[0][0][2] == pytest.approx(10.0)  # pnl

        # DB 写入
        assert len(written) == 1
        row = written[0]
        assert row[13] is True  # won
        assert row[12] == pytest.approx(10.0)  # price_change

    def test_sell_win(self):
        tracker = TradeOutcomeTracker()
        tracker.on_trade_opened(
            signal_id="sig-1", symbol="XAUUSD", timeframe="M5",
            strategy="sma_trend", direction="sell", fill_price=2010.0, confidence=0.70,
        )
        tracker.on_position_closed(_make_pos(action="sell"), close_price=2000.0)

        summary = tracker.summary()
        assert summary["total_evaluated"] == 1
        assert summary["total_wins"] == 1

    def test_buy_loss(self):
        tracker = TradeOutcomeTracker()
        tracker.on_trade_opened(
            signal_id="sig-1", symbol="XAUUSD", timeframe="M5",
            strategy="sma_trend", direction="buy", fill_price=2010.0, confidence=0.60,
        )
        tracker.on_position_closed(_make_pos(action="buy"), close_price=2000.0)

        summary = tracker.summary()
        assert summary["total_evaluated"] == 1
        assert summary["total_wins"] == 0


class TestUnresolvedClose:
    """close_price=None 时应记录 unresolved 终态而非静默丢弃。"""

    def test_none_close_price_records_unresolved(self):
        written: List[Any] = []
        outcomes: List[Any] = []
        tracker = TradeOutcomeTracker(
            write_fn=lambda rows: written.extend(rows),
            on_outcome_fn=lambda *a, **kw: outcomes.append((a, kw)),
        )
        tracker.on_trade_opened(
            signal_id="sig-1", symbol="XAUUSD", timeframe="M5",
            strategy="sma_trend", direction="buy", fill_price=2000.0, confidence=0.75,
        )
        tracker.on_position_closed(_make_pos(), close_price=None)

        summary = tracker.summary()
        assert summary["unresolved_closes"] == 1
        assert summary["total_evaluated"] == 0
        assert summary["active_trades"] == 0  # 交易已从 active 中移除

        # 绩效回调不应触发（无法判断盈亏）
        assert len(outcomes) == 0

        # DB 仍写入（便于事后审计）
        assert len(written) == 1
        row = written[0]
        assert row[11] is None  # close_price
        assert row[12] is None  # price_change
        assert row[13] is None  # won
        meta = row[15]
        assert meta["close_source"] == "mt5_missing"

    def test_invalid_close_price_records_unresolved(self):
        tracker = TradeOutcomeTracker()
        tracker.on_trade_opened(
            signal_id="sig-1", symbol="XAUUSD", timeframe="M5",
            strategy="sma_trend", direction="buy", fill_price=2000.0, confidence=0.75,
        )
        tracker.on_position_closed(_make_pos(), close_price="not_a_number")

        summary = tracker.summary()
        assert summary["unresolved_closes"] == 1
        assert summary["total_evaluated"] == 0


class TestCloseSource:
    """close_source 从 pos._close_source 传递到 DB 记录。"""

    def test_history_deals_source(self):
        written: List[Any] = []
        tracker = TradeOutcomeTracker(write_fn=lambda rows: written.extend(rows))
        tracker.on_trade_opened(
            signal_id="sig-1", symbol="XAUUSD", timeframe="M5",
            strategy="sma_trend", direction="buy", fill_price=2000.0, confidence=0.75,
        )
        tracker.on_position_closed(
            _make_pos(close_source="history_deals"), close_price=2005.0,
        )

        meta = written[0][15]
        assert meta["close_source"] == "history_deals"

    def test_manual_reconcile_source(self):
        written: List[Any] = []
        tracker = TradeOutcomeTracker(write_fn=lambda rows: written.extend(rows))
        tracker.on_trade_opened(
            signal_id="sig-1", symbol="XAUUSD", timeframe="M5",
            strategy="sma_trend", direction="buy", fill_price=2000.0, confidence=0.75,
        )
        tracker.on_position_closed(
            _make_pos(close_source="manual_reconcile"), close_price=2005.0,
        )

        meta = written[0][15]
        assert meta["close_source"] == "manual_reconcile"

    def test_default_source_when_not_set(self):
        written: List[Any] = []
        tracker = TradeOutcomeTracker(write_fn=lambda rows: written.extend(rows))
        tracker.on_trade_opened(
            signal_id="sig-1", symbol="XAUUSD", timeframe="M5",
            strategy="sma_trend", direction="buy", fill_price=2000.0, confidence=0.75,
        )
        # pos 没有 _close_source 属性
        tracker.on_position_closed(_make_pos(), close_price=2005.0)

        meta = written[0][15]
        assert meta["close_source"] == "position_closed"

    def test_unresolved_always_uses_mt5_missing(self):
        """即使 pos._close_source="history_deals"，close_price=None 时强制 mt5_missing。"""
        written: List[Any] = []
        tracker = TradeOutcomeTracker(write_fn=lambda rows: written.extend(rows))
        tracker.on_trade_opened(
            signal_id="sig-1", symbol="XAUUSD", timeframe="M5",
            strategy="sma_trend", direction="buy", fill_price=2000.0, confidence=0.75,
        )
        tracker.on_position_closed(
            _make_pos(close_source="history_deals"), close_price=None,
        )

        meta = written[0][15]
        assert meta["close_source"] == "mt5_missing"


class TestEdgeCases:
    """边界情况。"""

    def test_unknown_signal_id_no_error(self):
        """未注册的 signal_id 不报错。"""
        tracker = TradeOutcomeTracker()
        tracker.on_position_closed(_make_pos(signal_id="unknown"), close_price=2000.0)
        assert tracker.summary()["total_evaluated"] == 0

    def test_empty_signal_id_skipped(self):
        tracker = TradeOutcomeTracker()
        tracker.on_position_closed(_make_pos(signal_id=""), close_price=2000.0)
        assert tracker.summary()["total_evaluated"] == 0

    def test_multiple_trades_mixed_outcomes(self):
        tracker = TradeOutcomeTracker()
        # 交易 1: 正常关仓
        tracker.on_trade_opened(
            signal_id="sig-1", symbol="XAUUSD", timeframe="M5",
            strategy="sma_trend", direction="buy", fill_price=2000.0, confidence=0.75,
        )
        # 交易 2: unresolved
        tracker.on_trade_opened(
            signal_id="sig-2", symbol="XAUUSD", timeframe="M5",
            strategy="rsi_reversion", direction="sell", fill_price=2010.0, confidence=0.80,
        )

        tracker.on_position_closed(_make_pos(signal_id="sig-1", action="buy"), close_price=2010.0)
        tracker.on_position_closed(_make_pos(signal_id="sig-2", action="sell"), close_price=None)

        summary = tracker.summary()
        assert summary["total_evaluated"] == 1
        assert summary["total_wins"] == 1
        assert summary["unresolved_closes"] == 1
        assert summary["active_trades"] == 0


# ── §0y P2 回归：close_source 必须从公开 close_source 读，而非废弃的 _close_source ──


def _make_pos_with_real_close_source(
    *,
    signal_id: str = "sig-1",
    symbol: str = "XAUUSD",
    action: str = "buy",
    close_source: Optional[str] = None,
) -> SimpleNamespace:
    """使用 TrackedPosition 实际公开字段 ``close_source`` 而非 _close_source。

    参 src/trading/positions/manager.py:116 (字段定义) +
    src/trading/positions/reconciliation.py:376 (赋值点)。
    """
    pos = SimpleNamespace(signal_id=signal_id, symbol=symbol, action=action)
    if close_source is not None:
        pos.close_source = close_source
    return pos


def test_close_source_read_from_public_attr_history_deals() -> None:
    """P2 §0y 回归：reconciliation 写 pos.close_source；旧 trade_outcome.py
    从 pos._close_source 读 → 真实平仓来源被丢失，落库 metadata 默认
    'position_closed'，污染 trade_outcomes 审计事实。
    """
    written: List[Any] = []
    tracker = TradeOutcomeTracker(write_fn=lambda rows: written.extend(rows))
    tracker.on_trade_opened(
        signal_id="sig-1",
        symbol="XAUUSD",
        timeframe="M5",
        strategy="sma_trend",
        direction="buy",
        fill_price=2000.0,
        confidence=0.75,
    )
    pos = _make_pos_with_real_close_source(
        action="buy", close_source="history_deals"
    )
    tracker.on_position_closed(pos, close_price=2005.0)

    meta = written[0][15]
    assert meta["close_source"] == "history_deals", (
        f"必须从 pos.close_source 读真实来源；got close_source={meta['close_source']!r}, "
        f"meta={meta!r}"
    )


def test_close_source_read_from_public_attr_mt5_missing() -> None:
    """对称契约：reconciliation 写 mt5_missing 时也必须被读出。"""
    written: List[Any] = []
    tracker = TradeOutcomeTracker(write_fn=lambda rows: written.extend(rows))
    tracker.on_trade_opened(
        signal_id="sig-2",
        symbol="XAUUSD",
        timeframe="M5",
        strategy="sma_trend",
        direction="sell",
        fill_price=2000.0,
        confidence=0.6,
    )
    pos = _make_pos_with_real_close_source(
        signal_id="sig-2", action="sell", close_source="mt5_missing"
    )
    tracker.on_position_closed(pos, close_price=2005.0)

    meta = written[0][15]
    assert meta["close_source"] == "mt5_missing"
