"""OutcomeTracker 单元测试。

重点覆盖以下场景：
1. 趋势行情：连续 confirmed_buy/sell → bars_elapsed 正确推进，不堆积
2. 普通场景：confirmed_buy 后 N 根非 buy 事件触发评估
3. confirmed_cancelled 仍然推进计数
4. 多策略 key 隔离
5. max_pending 上限淘汰机制
6. close_price 提取路径：metadata 注入（runtime 路径）优先于 indicators 扫描
7. RSI/MACD/Supertrend 等无 close 字段的策略通过 metadata 获得 close_price
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
    use_metadata_close: bool = False,  # simulate runtime-injected close_price
    indicators_override: Optional[dict] = None,
) -> SimpleNamespace:
    """构建模拟 SignalEvent。

    Parameters
    ----------
    use_metadata_close:
        True → close 放在 metadata["close_price"]（模拟 runtime 注入路径）；
        False → close 放在 indicators["sma20"]["close"]（旧路径兜底）。
    indicators_override:
        完全替换 indicators dict（用于模拟只有 rsi/macd payload 的场景）。
    """
    meta: dict = {
        "signal_state": signal_state,
        "scope": scope,
        "bar_time": datetime.now(timezone.utc).isoformat(),
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


class TestOutcomeTrackerClosePriceExtraction:
    """close_price 提取路径的鲁棒性测试。

    根本问题：SignalEvent.indicators 是策略域收窄后的副本，
    RSI/MACD/Supertrend 等策略的 payload 不含 close 字段，
    原来的硬编码名称列表（boll20/sma20/ema50/ema200/atr14）导致
    大多数策略的 entry_price/exit_price = None → won = NULL → 被 fetch_winrates 过滤。
    修复：runtime 在策略循环前从全量 snapshot 提取 close 并注入 metadata["close_price"]。
    """

    def test_metadata_close_price_used_for_rsi_style_strategy(self):
        """RSI 策略：indicators 只有 rsi/atr payload，无 close 字段。
        必须通过 metadata["close_price"] 获取价格（runtime 注入路径）。
        """
        rows_written: List[Tuple] = []
        tracker = OutcomeTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=1,
        )

        rsi_only_indicators = {
            "rsi14": {"rsi": 72.5},
            "atr14": {"atr": 0.0012},
        }

        entry_event = _make_event(
            close=1.1000,
            signal_id="rsi-entry",
            use_metadata_close=True,
            indicators_override=rsi_only_indicators,
        )
        exit_event = _make_event(
            close=1.1015,
            signal_id="rsi-exit",
            use_metadata_close=True,
            indicators_override=rsi_only_indicators,
        )

        tracker.on_signal_event(entry_event)
        tracker.on_signal_event(exit_event)

        assert len(rows_written) == 1, (
            "RSI strategy outcome should be evaluated via metadata close_price"
        )
        row = rows_written[0]
        entry_price = row[7]
        exit_price = row[8]
        won = row[10]
        assert entry_price == pytest.approx(1.1000), "entry_price must not be None"
        assert exit_price == pytest.approx(1.1015), "exit_price must not be None"
        assert won is True

    def test_metadata_close_takes_priority_over_indicators_close(self):
        """metadata["close_price"] 优先级高于 indicators payload 中的 close。"""
        rows_written: List[Tuple] = []
        tracker = OutcomeTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=1,
        )

        # metadata 中有 1.2000，indicators payload 中有 1.1000
        # 应使用 metadata 的值
        event_entry = SimpleNamespace(
            symbol="EURUSD", timeframe="H1", strategy="test",
            action="buy", confidence=0.8, signal_id="e1",
            generated_at=datetime.now(timezone.utc),
            metadata={
                "signal_state": "confirmed_buy", "scope": "confirmed",
                "bar_time": datetime.now(timezone.utc).isoformat(),
                "regime": "trending",
                "close_price": 1.2000,  # runtime-injected
            },
            indicators={"boll20": {"close": 1.1000, "bb_mid": 1.0990}},
        )
        event_exit = SimpleNamespace(
            symbol="EURUSD", timeframe="H1", strategy="test",
            action="buy", confidence=0.8, signal_id="e2",
            generated_at=datetime.now(timezone.utc),
            metadata={
                "signal_state": "confirmed_buy", "scope": "confirmed",
                "bar_time": datetime.now(timezone.utc).isoformat(),
                "regime": "trending",
                "close_price": 1.2050,
            },
            indicators={"boll20": {"close": 1.1010, "bb_mid": 1.1000}},
        )

        tracker.on_signal_event(event_entry)
        tracker.on_signal_event(event_exit)

        assert len(rows_written) == 1
        assert rows_written[0][7] == pytest.approx(1.2000)  # entry from metadata
        assert rows_written[0][8] == pytest.approx(1.2050)  # exit from metadata

    def test_indicators_close_fallback_when_no_metadata(self):
        """当 metadata 不含 close_price 时，回退到 indicators payload 扫描。"""
        rows_written: List[Tuple] = []
        tracker = OutcomeTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=1,
        )

        # 旧路径：close 来自 boll20 payload
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
        """无法从任何来源获取 exit_price 时，_tick_pending 提前返回，
        pending 条目不被评估（无 exit 价格则无法判断输赢）。
        """
        rows_written: List[Tuple] = []
        tracker = OutcomeTracker(
            write_fn=lambda rows: rows_written.extend(rows),
            bars_to_evaluate=1,
        )

        no_close_indicators = {"rsi14": {"rsi": 68.0}}
        entry = _make_event(close=1.0, signal_id="nc-entry",
                            indicators_override=no_close_indicators)
        exit_ = _make_event(close=1.0, signal_id="nc-exit",
                            indicators_override=no_close_indicators)
        # 两个事件的 metadata 均不含 close_price

        tracker.on_signal_event(entry)
        tracker.on_signal_event(exit_)

        # exit_price=None 时 _tick_pending 提前返回，没有写入任何行
        assert len(rows_written) == 0
        # pending 条目仍存在（未被评估，由 max_pending 兜底淘汰）
        total_pending = sum(len(v) for v in tracker._pending.values())
        assert total_pending >= 1
