"""SignalQualityTracker + TradeOutcomeTracker 单元测试。

## SignalQualityTracker 重点覆盖场景
1. 趋势行情：连续 confirmed_buy/sell → bars_elapsed 正确推进，不堆积
2. 普通场景：confirmed_buy 后 N 根非 buy 事件触发评估
3. confirmed_cancelled 仍然推进计数
4. 多策略 key 隔离
5. max_pending 上限淘汰机制
6. close_price 提取路径：metadata 注入（runtime 路径）优先于 indicators 扫描
7. RSI/MACD/Supertrend 等无 close 字段的策略通过 metadata 获得 close_price

## TradeOutcomeTracker 重点覆盖场景
8. on_trade_opened 登记 + on_position_closed 评估
9. 无 signal_id 的仓位被跳过
10. 未登记的仓位关闭不报错
11. on_outcome_fn 回调正确触发（含 source="trade"）
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import List, Tuple, Optional
from unittest.mock import MagicMock

from src.trading.signal_quality_tracker import SignalQualityTracker
from src.trading.trade_outcome_tracker import TradeOutcomeTracker

# 全局递增计数器，确保每个 _make_event 调用产生唯一的 bar_time。
# Windows 上 datetime.now() 快速连续调用可能返回相同值（~15ms 分辨率），
# 而 SignalQualityTracker._advance_pending 依赖 bar_time 去重，
# 相同 bar_time 的事件只推进一次 bars_elapsed。测试中每个事件代表不同 bar，
# 因此必须保证 bar_time 唯一。
_event_counter: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(
    *,
    symbol: str = "EURUSD",
    timeframe: str = "H1",
    strategy: str = "test_strategy",
    signal_state: str = "confirmed_buy",
    scope: str = "confirmed",
    action: str = "buy",
    confidence: float = 0.80,
    close: float = 1.1000,
    signal_id: str = "sig-001",
    regime: str = "trending",
    use_metadata_close: bool = False,
    indicators_override: Optional[dict] = None,
) -> SimpleNamespace:
    """构建模拟 SignalEvent。每次调用产生不同 bar_time（模拟不同 bar 收盘）。"""
    global _event_counter
    _event_counter += 1
    # 递增秒数，保证每个事件有不同的 bar_time
    from datetime import timedelta
    unique_bar_time = (
        datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=_event_counter)
    )
    meta: dict = {
        "signal_state": signal_state,
        "scope": scope,
        "bar_time": unique_bar_time.isoformat(),
        "regime": regime,
    }
    if use_metadata_close:
        meta["close_price"] = close
        indicators: dict = indicators_override if indicators_override is not None else {}
    else:
        indicators = indicators_override if indicators_override is not None else {
            "sma20": {"close": close},
        }

    return SimpleNamespace(
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy,
        action=action,
        confidence=confidence,
        signal_id=signal_id,
        generated_at=datetime.now(timezone.utc),
        metadata=meta,
        indicators=indicators,
    )


# ---------------------------------------------------------------------------
# SignalQualityTracker Tests
# ---------------------------------------------------------------------------

class TestSignalQualityTrackerTrendScenario:
    """趋势行情：连续 confirmed_buy 信号，bars_elapsed 必须正确推进。"""

    def test_repeated_confirmed_buy_ticks_pending(self):
        """bug 复现：三根 confirmed_buy bar 后，第一笔 pending 必须被评估。"""
        rows_written: List[Tuple] = []
        tracker = SignalQualityTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=3,
        )

        prices = [1.1000, 1.1010, 1.1020, 1.1030]
        for i, price in enumerate(prices):
            event = _make_event(
                signal_id=f"sig-{i:03d}",
                signal_state="confirmed_buy",
                action="buy",
                close=price,
            )
            tracker.on_signal_event(event)

        assert len(rows_written) == 1
        row = rows_written[0]
        assert row[1] == "sig-000"
        assert row[11] == 3

    def test_win_correctly_detected_on_trend_buy(self):
        """价格上涨时 buy 信号的 won 应为 True。"""
        rows_written: List[Tuple] = []
        tracker = SignalQualityTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=2,
        )
        tracker.on_signal_event(_make_event(close=1.1000, signal_id="sig-a"))
        tracker.on_signal_event(_make_event(close=1.1010, signal_id="sig-b"))
        tracker.on_signal_event(_make_event(close=1.1020, signal_id="sig-c"))

        assert len(rows_written) >= 1
        assert rows_written[0][10] is True

    def test_loss_detected_on_trend_sell(self):
        """卖出信号，价格上涨时 won 应为 False。"""
        rows_written: List[Tuple] = []
        tracker = SignalQualityTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=2,
        )
        tracker.on_signal_event(
            _make_event(close=1.1020, signal_state="confirmed_sell", action="sell", signal_id="s0")
        )
        tracker.on_signal_event(
            _make_event(close=1.1025, signal_state="confirmed_sell", action="sell", signal_id="s1")
        )
        tracker.on_signal_event(
            _make_event(close=1.1030, signal_state="confirmed_sell", action="sell", signal_id="s2")
        )

        assert len(rows_written) >= 1
        assert rows_written[0][10] is False

    def test_no_infinite_accumulation(self):
        """N 根 confirmed_buy 后 pending 不应无限堆积。"""
        rows_written: List[Tuple] = []
        tracker = SignalQualityTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=3,
        )
        n = 10
        for i in range(n):
            tracker.on_signal_event(_make_event(close=1.1000 + i * 0.001, signal_id=f"s{i}"))

        total_pending = sum(len(v) for v in tracker._pending.values())
        assert total_pending < n
        assert len(rows_written) > 0


class TestSignalQualityTrackerCancelledAdvancesPending:
    """confirmed_cancelled 事件也应推进 bars_elapsed。"""

    def test_cancelled_ticks_pending(self):
        rows_written: List[Tuple] = []
        tracker = SignalQualityTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=2,
        )
        tracker.on_signal_event(_make_event(close=1.1000, signal_id="orig"))
        tracker.on_signal_event(
            _make_event(close=1.1010, signal_state="confirmed_cancelled", action="hold", signal_id="c1")
        )
        tracker.on_signal_event(
            _make_event(close=1.1020, signal_state="confirmed_cancelled", action="hold", signal_id="c2")
        )

        assert len(rows_written) == 1
        assert rows_written[0][1] == "orig"


class TestSignalQualityTrackerIntrabarIgnored:
    """scope != confirmed 的事件应被忽略。"""

    def test_intrabar_events_do_not_tick(self):
        rows_written: List[Tuple] = []
        tracker = SignalQualityTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=1,
        )
        tracker.on_signal_event(_make_event(close=1.1000, signal_id="pending"))
        for _ in range(5):
            tracker.on_signal_event(
                _make_event(close=1.1010, scope="intrabar", signal_state="preview_buy")
            )
        assert len(rows_written) == 0
        total_pending = sum(len(v) for v in tracker._pending.values())
        assert total_pending == 1


class TestSignalQualityTrackerMultiStrategy:
    """不同品种的 pending 互相隔离。

    注意：同一 (symbol, timeframe) 下的不同策略共享 bar 计数，
    因为 bar close 是共享物理事件。真正的隔离在 symbol/timeframe 维度。
    """

    def test_different_symbols_are_isolated(self):
        rows_written: List[Tuple] = []
        tracker = SignalQualityTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=2,
        )
        tracker.on_signal_event(_make_event(symbol="EURUSD", close=1.1000, signal_id="a0"))
        tracker.on_signal_event(_make_event(symbol="XAUUSD", close=2000.0, signal_id="b0"))

        # 只推进 EURUSD 两次
        tracker.on_signal_event(_make_event(symbol="EURUSD", close=1.1010, signal_id="a1"))
        tracker.on_signal_event(_make_event(symbol="EURUSD", close=1.1020, signal_id="a2"))

        evaluated_signals = [r[1] for r in rows_written]
        assert "a0" in evaluated_signals
        assert "b0" not in evaluated_signals


class TestSignalQualityTrackerMaxPending:
    """超出 max_pending 时丢弃最旧的条目。"""

    def test_max_pending_evicts_oldest(self):
        rows_written: List[Tuple] = []
        tracker = SignalQualityTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=100,
            max_pending=3,
        )
        for i in range(5):
            tracker.on_signal_event(_make_event(close=1.1000, signal_id=f"s{i}"))

        key = ("EURUSD", "H1", "test_strategy")
        with tracker._lock:
            lst = tracker._pending.get(key, [])
        assert len(lst) <= 3


class TestSignalQualityTrackerWinrateStats:
    """winrate_summary 统计正确。"""

    def test_winrate_summary_after_evaluation(self):
        rows_written: List[Tuple] = []
        tracker = SignalQualityTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=1,
        )
        tracker.on_signal_event(_make_event(close=1.1000, signal_id="w1"))
        tracker.on_signal_event(_make_event(close=1.1010, signal_id="w2"))
        tracker.on_signal_event(_make_event(close=1.1020, signal_id="w3"))

        summary = tracker.winrate_summary()
        assert summary["total_evaluated"] >= 1
        assert summary["win_rate"] == 1.0


class TestSignalQualityTrackerClosePriceExtraction:
    """close_price 提取路径的鲁棒性测试。"""

    def test_metadata_close_price_used_for_rsi_style_strategy(self):
        rows_written: List[Tuple] = []
        tracker = SignalQualityTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=1,
        )

        rsi_only_indicators = {"rsi14": {"rsi": 72.5}, "atr14": {"atr": 0.0012}}
        entry = _make_event(close=1.1000, signal_id="rsi-entry",
                            use_metadata_close=True, indicators_override=rsi_only_indicators)
        exit_ = _make_event(close=1.1015, signal_id="rsi-exit",
                            use_metadata_close=True, indicators_override=rsi_only_indicators)

        tracker.on_signal_event(entry)
        tracker.on_signal_event(exit_)

        assert len(rows_written) == 1
        assert rows_written[0][7] == pytest.approx(1.1000)
        assert rows_written[0][8] == pytest.approx(1.1015)
        assert rows_written[0][10] is True

    def test_metadata_close_takes_priority_over_indicators_close(self):
        rows_written: List[Tuple] = []
        tracker = SignalQualityTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=1,
        )

        from datetime import timedelta
        bar_t1 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        bar_t2 = bar_t1 + timedelta(hours=1)
        event_entry = SimpleNamespace(
            symbol="EURUSD", timeframe="H1", strategy="test",
            action="buy", confidence=0.8, signal_id="e1",
            generated_at=datetime.now(timezone.utc),
            metadata={
                "signal_state": "confirmed_buy", "scope": "confirmed",
                "bar_time": bar_t1.isoformat(),
                "regime": "trending", "close_price": 1.2000,
            },
            indicators={"boll20": {"close": 1.1000, "bb_mid": 1.0990}},
        )
        event_exit = SimpleNamespace(
            symbol="EURUSD", timeframe="H1", strategy="test",
            action="buy", confidence=0.8, signal_id="e2",
            generated_at=datetime.now(timezone.utc),
            metadata={
                "signal_state": "confirmed_buy", "scope": "confirmed",
                "bar_time": bar_t2.isoformat(),
                "regime": "trending", "close_price": 1.2050,
            },
            indicators={"boll20": {"close": 1.1010, "bb_mid": 1.1000}},
        )

        tracker.on_signal_event(event_entry)
        tracker.on_signal_event(event_exit)

        assert len(rows_written) == 1
        assert rows_written[0][7] == pytest.approx(1.2000)
        assert rows_written[0][8] == pytest.approx(1.2050)

    def test_indicators_close_fallback_when_no_metadata(self):
        rows_written: List[Tuple] = []
        tracker = SignalQualityTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=1,
        )

        entry = _make_event(close=1.1000, signal_id="b-entry", use_metadata_close=False,
                            indicators_override={"boll20": {"close": 1.1000, "bb_mid": 1.0990}})
        exit_ = _make_event(close=1.1010, signal_id="b-exit", use_metadata_close=False,
                            indicators_override={"boll20": {"close": 1.1010, "bb_mid": 1.1000}})

        tracker.on_signal_event(entry)
        tracker.on_signal_event(exit_)

        assert len(rows_written) == 1
        assert rows_written[0][7] == pytest.approx(1.1000)
        assert rows_written[0][8] == pytest.approx(1.1010)

    def test_no_close_anywhere_skips_evaluation(self):
        rows_written: List[Tuple] = []
        tracker = SignalQualityTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=1,
        )

        no_close_indicators = {"rsi14": {"rsi": 68.0}}
        entry = _make_event(close=1.0, signal_id="nc-entry",
                            indicators_override=no_close_indicators)
        exit_ = _make_event(close=1.0, signal_id="nc-exit",
                            indicators_override=no_close_indicators)

        tracker.on_signal_event(entry)
        tracker.on_signal_event(exit_)

        assert len(rows_written) == 0
        total_pending = sum(len(v) for v in tracker._pending.values())
        assert total_pending >= 1


# ---------------------------------------------------------------------------
# TradeOutcomeTracker Tests
# ---------------------------------------------------------------------------

class TestTradeOutcomeTrackerBasic:
    """基本开仓 → 关仓评估流程。"""

    def test_buy_win(self):
        """买入后价格上涨 → won=True"""
        rows_written: List[Tuple] = []
        tracker = TradeOutcomeTracker(
            write_fn=lambda rows: rows_written.extend(rows),
        )

        tracker.on_trade_opened(
            signal_id="t-001",
            symbol="XAUUSD",
            timeframe="M1",
            strategy="sma_trend",
            action="buy",
            fill_price=2000.0,
            confidence=0.85,
            regime="trending",
        )

        pos = SimpleNamespace(signal_id="t-001", symbol="XAUUSD", action="buy")
        tracker.on_position_closed(pos, close_price=2010.0)

        assert len(rows_written) == 1
        row = rows_written[0]
        assert row[1] == "t-001"         # signal_id
        assert row[7] == 2000.0          # fill_price
        assert row[8] == 2010.0          # close_price
        assert row[9] == pytest.approx(10.0)  # price_change
        assert row[10] is True           # won

    def test_sell_loss(self):
        """卖出后价格上涨 → won=False"""
        rows_written: List[Tuple] = []
        tracker = TradeOutcomeTracker(
            write_fn=lambda rows: rows_written.extend(rows),
        )

        tracker.on_trade_opened(
            signal_id="t-002",
            symbol="XAUUSD",
            timeframe="M1",
            strategy="rsi_reversion",
            action="sell",
            fill_price=2000.0,
            confidence=0.75,
        )

        pos = SimpleNamespace(signal_id="t-002", symbol="XAUUSD", action="sell")
        tracker.on_position_closed(pos, close_price=2005.0)

        assert len(rows_written) == 1
        assert rows_written[0][10] is False


class TestTradeOutcomeTrackerEdgeCases:
    """边界场景。"""

    def test_close_without_open_is_noop(self):
        """未登记的仓位关闭 → 无异常，不写入。"""
        rows_written: List[Tuple] = []
        tracker = TradeOutcomeTracker(
            write_fn=lambda rows: rows_written.extend(rows),
        )

        pos = SimpleNamespace(signal_id="unknown-id", symbol="XAUUSD", action="buy")
        tracker.on_position_closed(pos, close_price=2000.0)

        assert len(rows_written) == 0

    def test_close_with_none_price_records_unresolved(self):
        """close_price=None → 记录 unresolved 终态（写入 DB，从 active 移除）。"""
        rows_written: List[Tuple] = []
        tracker = TradeOutcomeTracker(
            write_fn=lambda rows: rows_written.extend(rows),
        )

        tracker.on_trade_opened(
            signal_id="t-003", symbol="XAUUSD", timeframe="M1",
            strategy="test", action="buy", fill_price=2000.0, confidence=0.8,
        )

        pos = SimpleNamespace(signal_id="t-003", symbol="XAUUSD", action="buy")
        tracker.on_position_closed(pos, close_price=None)

        # 写入 DB（unresolved 记录，便于事后审计）
        assert len(rows_written) == 1
        row = rows_written[0]
        assert row[8] is None  # close_price
        assert row[10] is None  # won
        assert row[12]["close_source"] == "mt5_missing"
        # 交易已从 active 移除
        assert "t-003" not in tracker._active
        # unresolved 计数增加
        assert tracker.summary()["unresolved_closes"] == 1

    def test_no_signal_id_on_pos_is_skipped(self):
        """仓位没有 signal_id → 跳过。"""
        tracker = TradeOutcomeTracker()
        pos = SimpleNamespace(symbol="XAUUSD", action="buy")  # no signal_id
        tracker.on_position_closed(pos, close_price=2000.0)  # no error

    def test_restore_tracked_position_rehydrates_active_trade(self):
        tracker = TradeOutcomeTracker()

        tracker.restore_tracked_position(
            SimpleNamespace(
                signal_id="restored-1",
                symbol="XAUUSD",
                timeframe="M5",
                strategy="sma_trend",
                action="buy",
                entry_price=2001.5,
                confidence=0.82,
                regime="trend",
                opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
        tracker.on_position_closed(
            SimpleNamespace(signal_id="restored-1", symbol="XAUUSD", action="buy"),
            close_price=2005.0,
        )

        summary = tracker.summary()
        assert summary["total_evaluated"] == 1
        assert summary["total_wins"] == 1
        assert summary["active_trades"] == 0


class TestTradeOutcomeTrackerCallback:
    """on_outcome_fn 回调验证。"""

    def test_callback_receives_source_trade(self):
        """确认 on_outcome_fn 收到 source='trade'。"""
        callback_calls: List[dict] = []

        def mock_callback(strategy, won, pnl, *, regime=None, source="signal"):
            callback_calls.append({
                "strategy": strategy,
                "won": won,
                "pnl": pnl,
                "regime": regime,
                "source": source,
            })

        tracker = TradeOutcomeTracker(on_outcome_fn=mock_callback)

        tracker.on_trade_opened(
            signal_id="t-cb",
            symbol="XAUUSD",
            timeframe="M1",
            strategy="ema_ribbon",
            action="buy",
            fill_price=2000.0,
            confidence=0.9,
            regime="trending",
        )

        pos = SimpleNamespace(signal_id="t-cb", symbol="XAUUSD", action="buy")
        tracker.on_position_closed(pos, close_price=2020.0)

        assert len(callback_calls) == 1
        assert callback_calls[0]["strategy"] == "ema_ribbon"
        assert callback_calls[0]["won"] is True
        assert callback_calls[0]["pnl"] == pytest.approx(20.0)
        assert callback_calls[0]["regime"] == "trending"
        assert callback_calls[0]["source"] == "trade"


class TestTradeOutcomeTrackerSummary:
    """summary() 统计。"""

    def test_summary_tracks_wins_and_losses(self):
        tracker = TradeOutcomeTracker()

        # 一笔赢
        tracker.on_trade_opened(
            signal_id="w1", symbol="XAUUSD", timeframe="M1",
            strategy="test", action="buy", fill_price=2000.0, confidence=0.8,
        )
        tracker.on_position_closed(
            SimpleNamespace(signal_id="w1"), close_price=2010.0,
        )

        # 一笔亏
        tracker.on_trade_opened(
            signal_id="l1", symbol="XAUUSD", timeframe="M1",
            strategy="test", action="buy", fill_price=2000.0, confidence=0.8,
        )
        tracker.on_position_closed(
            SimpleNamespace(signal_id="l1"), close_price=1990.0,
        )

        summary = tracker.summary()
        assert summary["total_evaluated"] == 2
        assert summary["total_wins"] == 1
        assert summary["win_rate"] == 0.5
        assert summary["active_trades"] == 0
