"""PaperTradingBridge admission writeback (P9 bug #3) 单元测试。

验证 paper bridge 在每个 signal 处理路径都调用 admission_writer 回填
signal_events 的 actionability/guard_reason_code 字段。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.backtesting.paper_trading.bridge import PaperTradingBridge
from src.backtesting.paper_trading.config import PaperTradingConfig


@pytest.fixture
def writer_calls() -> list[tuple[str, str, str | None]]:
    return []


@pytest.fixture
def admission_writer(writer_calls):
    def _writer(signal_id, actionability, guard_reason_code):
        writer_calls.append((signal_id, actionability, guard_reason_code))

    return _writer


def _make_bridge(admission_writer) -> PaperTradingBridge:
    config = PaperTradingConfig(
        enabled=True,
        initial_balance=2000.0,
        max_positions=3,
        min_confidence=0.5,
        commission_per_lot=7.0,
        slippage_points=5.0,
    )
    return PaperTradingBridge(
        config=config,
        market_quote_fn=lambda symbol: None,
        admission_writer=admission_writer,
    )


def _signal(
    *,
    signal_id: str = "sig_1",
    confidence: float = 0.8,
    state: str = "confirmed_buy",
    indicators: Any = None,
):
    sig = MagicMock()
    sig.signal_id = signal_id
    sig.confidence = confidence
    sig.signal_state = state
    sig.symbol = "XAUUSD"
    sig.timeframe = "H1"
    sig.strategy = "test"
    sig.indicators = indicators if indicators is not None else {"atr14": {"value": 5.0}}
    sig.metadata = {}
    return sig


# ── admission_writer 必须被各处理分支调用 ──────────────────────


def test_writeback_called_with_hold_for_non_confirmed_state(
    writer_calls, admission_writer
):
    bridge = _make_bridge(admission_writer)
    bridge._running = True
    bridge._portfolio = (
        MagicMock()
    )  # 任意值，让 early return 之前 self._portfolio 非 None

    bridge.on_signal_event(_signal(state="preview_buy"))

    assert writer_calls == [("sig_1", "hold", "non_confirmed_state")]


def test_writeback_called_with_blocked_min_confidence_when_below_threshold(
    writer_calls, admission_writer
):
    bridge = _make_bridge(admission_writer)
    bridge._running = True
    bridge._portfolio = MagicMock()

    bridge.on_signal_event(_signal(confidence=0.3))  # < 0.5 threshold

    assert writer_calls == [("sig_1", "blocked", "min_confidence")]


def test_writeback_called_with_blocked_trade_params_when_atr_missing(
    writer_calls, admission_writer
):
    bridge = _make_bridge(admission_writer)
    bridge._running = True
    bridge._portfolio = MagicMock()

    bridge.on_signal_event(_signal(indicators={}))  # 无 atr14

    assert writer_calls == [("sig_1", "blocked", "trade_params_unavailable")]


def test_writeback_called_with_blocked_quote_stale_when_no_quote(
    writer_calls, admission_writer
):
    bridge = _make_bridge(admission_writer)
    bridge._running = True
    bridge._portfolio = MagicMock()
    # market_quote_fn 在 fixture 设为返回 None

    bridge.on_signal_event(_signal())

    assert writer_calls == [("sig_1", "blocked", "quote_stale")]


def test_writeback_skipped_silently_when_signal_id_missing(
    writer_calls, admission_writer
):
    """signal_id 为空 → admission_writer 不被调用（防御性）。"""
    bridge = _make_bridge(admission_writer)
    bridge._running = True
    bridge._portfolio = MagicMock()

    bridge.on_signal_event(_signal(signal_id=""))

    assert writer_calls == []


def test_writeback_swallows_exception_to_protect_signal_path(writer_calls):
    """admission_writer 抛异常不应中断 paper bridge 信号处理。"""

    def _broken_writer(*_args):
        raise RuntimeError("DB unavailable")

    bridge = _make_bridge(_broken_writer)
    bridge._running = True
    bridge._portfolio = MagicMock()

    # 不应抛出
    bridge.on_signal_event(_signal(confidence=0.3))


def test_no_writeback_called_when_admission_writer_is_none():
    """admission_writer=None 时静默跳过，不报错。"""
    config = PaperTradingConfig(
        enabled=True,
        initial_balance=2000.0,
        max_positions=3,
        min_confidence=0.5,
        commission_per_lot=7.0,
        slippage_points=5.0,
    )
    bridge = PaperTradingBridge(
        config=config,
        market_quote_fn=lambda symbol: None,
        admission_writer=None,
    )
    bridge._running = True
    bridge._portfolio = MagicMock()

    # 不应抛出
    bridge.on_signal_event(_signal())
