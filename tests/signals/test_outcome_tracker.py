"""OutcomeTracker 单元测试。

重点覆盖以下场景：
1. 趋势行情：连续 confirmed_buy/sell → bars_elapsed 正确推进，不堆积
2. 普通场景：confirmed_buy 后 N 根非 buy 事件触发评估
3. confirmed_cancelled 仍然推进计数
4. 多策略 key 隔离
5. max_pending 上限淘汰机制
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import List, Tuple, Optional
from unittest.mock import MagicMock

from src.signals.outcome_tracker import OutcomeTracker


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
) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        timeframe=timeframe,
        strategy=strategy,
        action=action,
        confidence=confidence,
        signal_id=signal_id,
        generated_at=datetime.now(timezone.utc),
        metadata={
            "signal_state": signal_state,
            "scope": scope,
            "bar_time": datetime.now(timezone.utc).isoformat(),
            "regime": regime,
        },
        indicators={
            "sma20": {"close": close},
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestOutcomeTrackerTrendScenario:
    """趋势行情：连续 confirmed_buy 信号，bars_elapsed 必须正确推进。"""

    def test_repeated_confirmed_buy_ticks_pending(self):
        """bug 复现：三根 confirmed_buy bar 后，第一笔 pending 必须被评估。"""
        rows_written: List[Tuple] = []
        tracker = OutcomeTracker(
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

        # 4 次事件：
        #   i=0: tick (no-op) + record sig-000 (bars_elapsed=0)
        #   i=1: tick sig-000 → elapsed=1; record sig-001
        #   i=2: tick sig-000 → elapsed=2, sig-001 → elapsed=1; record sig-002
        #   i=3: tick sig-000 → elapsed=3 → evaluate; sig-001 → 2; sig-002 → 1; record sig-003
        assert len(rows_written) == 1, (
            f"Expected 1 evaluated outcome, got {len(rows_written)}. "
            "Bug: _tick_pending was not called on confirmed_buy events."
        )
        row = rows_written[0]
        signal_id_col = row[1]
        bars_col = row[11]
        assert signal_id_col == "sig-000"
        assert bars_col == 3

    def test_win_correctly_detected_on_trend_buy(self):
        """价格上涨时 buy 信号的 won 应为 True。"""
        rows_written: List[Tuple] = []
        tracker = OutcomeTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=2,
        )
        # 记录进场
        tracker.on_signal_event(_make_event(close=1.1000, signal_id="sig-a"))
        # 推进两根 bar（价格上涨）
        tracker.on_signal_event(_make_event(close=1.1010, signal_id="sig-b"))
        tracker.on_signal_event(_make_event(close=1.1020, signal_id="sig-c"))

        assert len(rows_written) >= 1
        won = rows_written[0][10]  # won 列
        assert won is True

    def test_loss_detected_on_trend_sell(self):
        """卖出信号，价格上涨时 won 应为 False。"""
        rows_written: List[Tuple] = []
        tracker = OutcomeTracker(
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
        won = rows_written[0][10]
        assert won is False  # 卖出但价格上涨 → 亏损

    def test_no_infinite_accumulation(self):
        """N 根 confirmed_buy 后 pending 不应无限堆积（bars_to_evaluate=3）。"""
        rows_written: List[Tuple] = []
        tracker = OutcomeTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=3,
        )
        n = 10
        for i in range(n):
            tracker.on_signal_event(_make_event(close=1.1000 + i * 0.001, signal_id=f"s{i}"))

        # 10 根 bar：最多有 bars_to_evaluate 个 pending 未到期，其余都已评估
        total_pending = sum(len(v) for v in tracker._pending.values())
        assert total_pending < n, (
            f"Pending should not accumulate indefinitely; got {total_pending} pending after {n} bars"
        )
        assert len(rows_written) > 0, "At least some outcomes should have been evaluated"


class TestOutcomeTrackerCancelledAdvancesPending:
    """confirmed_cancelled 事件也应推进 bars_elapsed。"""

    def test_cancelled_ticks_pending(self):
        rows_written: List[Tuple] = []
        tracker = OutcomeTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=2,
        )
        # 记录 buy
        tracker.on_signal_event(_make_event(close=1.1000, signal_id="orig"))
        # 第一根 bar：cancelled（推进 elapsed=1）
        tracker.on_signal_event(
            _make_event(close=1.1010, signal_state="confirmed_cancelled", action="hold", signal_id="c1")
        )
        # 第二根 bar：cancelled（推进 elapsed=2 → 触发评估）
        tracker.on_signal_event(
            _make_event(close=1.1020, signal_state="confirmed_cancelled", action="hold", signal_id="c2")
        )

        assert len(rows_written) == 1
        assert rows_written[0][1] == "orig"


class TestOutcomeTrackerIntrabarIgnored:
    """scope != confirmed 的事件应被忽略。"""

    def test_intrabar_events_do_not_tick(self):
        rows_written: List[Tuple] = []
        tracker = OutcomeTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=1,
        )
        tracker.on_signal_event(_make_event(close=1.1000, signal_id="pending"))
        # 若干 intrabar 事件
        for _ in range(5):
            tracker.on_signal_event(
                _make_event(close=1.1010, scope="intrabar", signal_state="preview_buy")
            )
        # pending 仍在等待（intrabar 不推进计数）
        assert len(rows_written) == 0
        total_pending = sum(len(v) for v in tracker._pending.values())
        assert total_pending == 1


class TestOutcomeTrackerMultiStrategy:
    """不同策略的 pending 互相隔离。"""

    def test_strategies_are_isolated(self):
        rows_written: List[Tuple] = []
        tracker = OutcomeTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=2,
        )
        # 两个策略各自记录一笔 pending
        tracker.on_signal_event(_make_event(strategy="strat_a", close=1.1000, signal_id="a0"))
        tracker.on_signal_event(_make_event(strategy="strat_b", close=1.1000, signal_id="b0"))

        # 只推进 strat_a 两次
        tracker.on_signal_event(_make_event(strategy="strat_a", close=1.1010, signal_id="a1"))
        tracker.on_signal_event(_make_event(strategy="strat_a", close=1.1020, signal_id="a2"))

        # strat_a 的第一笔应已评估，strat_b 的 pending 仍在
        evaluated_signals = [r[1] for r in rows_written]
        assert "a0" in evaluated_signals
        assert "b0" not in evaluated_signals


class TestOutcomeTrackerMaxPending:
    """超出 max_pending 时丢弃最旧的条目。"""

    def test_max_pending_evicts_oldest(self):
        rows_written: List[Tuple] = []
        tracker = OutcomeTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=100,   # 设置很大，确保不会正常评估
            max_pending=3,
        )
        for i in range(5):
            tracker.on_signal_event(_make_event(close=1.1000, signal_id=f"s{i}"))

        key = ("EURUSD", "H1", "test_strategy")
        with tracker._lock:
            lst = tracker._pending.get(key, [])
        # 最多保留 max_pending 个
        assert len(lst) <= 3


class TestOutcomeTrackerWinrateStats:
    """winrate_summary 统计正确。"""

    def test_winrate_summary_after_evaluation(self):
        rows_written: List[Tuple] = []
        tracker = OutcomeTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=1,
        )
        # 两笔 buy，价格上涨（两笔都赢）
        tracker.on_signal_event(_make_event(close=1.1000, signal_id="w1"))
        tracker.on_signal_event(_make_event(close=1.1010, signal_id="w2"))
        tracker.on_signal_event(_make_event(close=1.1020, signal_id="w3"))

        summary = tracker.winrate_summary()
        assert summary["total_evaluated"] >= 1
        assert summary["win_rate"] is not None
        assert summary["win_rate"] == 1.0  # 所有 buy 信号都赢了
