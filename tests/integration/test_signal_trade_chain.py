"""全链路集成测试：indicator snapshot → signal 状态机 → TradeExecutor 下单

测试范围：
  1. SmaTrendStrategy buy 信号：confirmed snapshot → TradeExecutor 触发下单
  2. RsiReversionStrategy sell 信号：confirmed snapshot → TradeExecutor 触发下单
  3. require_armed 完整流程：intrabar(preview) → intrabar(armed) → confirmed → 下单
  4. 熔断器：连续失败 N 次后暂停，reset 后恢复
  5. close_position 竞态修复验证：order_send=None 且持仓已消失 → 视为成功
  6. 会话过滤验证：非允许时段不触发信号
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.signals.models import SignalDecision, SignalEvent
from src.signals.orchestration import SignalPolicy, SignalRuntime, SignalTarget
from src.signals.evaluation.indicators_helpers import extract_close_price
from src.signals.service import SignalModule
from src.signals.strategies.adapters import IndicatorSource
from src.signals.strategies.legacy.trend import SmaTrendStrategy
from src.signals.strategies.legacy.mean_reversion import RsiReversionStrategy
from src.trading.execution import ExecutorConfig, TradeExecutor


# ---------------------------------------------------------------------------
# 通用指标快照构造器
# ---------------------------------------------------------------------------

def _trending_buy_indicators(close: float = 3000.0, atr: float = 5.0) -> Dict[str, Dict[str, float]]:
    """构造触发趋势策略 buy 信号 + TRENDING regime 的指标快照。

    关键：BB 宽度 > KC 宽度（bb_upper > kc_upper），确保无 Keltner-Bollinger Squeeze，
    regime 由 ADX≥25 决定为 TRENDING，sma_trend 的 affinity=1.0，confidence 不被压制。
    """
    return {
        "sma20":    {"sma": close + 10.0, "close": close},    # fast >> slow → clear buy
        "ema50":    {"ema": close - 5.0},
        "atr14":    {"atr": atr},
        "adx14":    {"adx": 30.0, "plus_di": 28.0, "minus_di": 15.0},  # ADX≥25 → TRENDING
        "boll20":   {"bb_upper": close + 30, "bb_lower": close - 30, "bb_mid": close, "close": close},
        "keltner20":{"kc_upper": close + 20, "kc_lower": close - 20},  # KC < BB → 无 Squeeze
        "rsi14":    {"rsi": 55.0},
    }


def _ranging_rsi_buy_indicators(close: float = 3000.0, atr: float = 5.0) -> Dict[str, Dict[str, float]]:
    """构造触发 RSI 超卖 buy 信号 + RANGING regime 的指标快照。

    关键：BB > KC（无 Squeeze），ADX<20 → RANGING，rsi_reversion affinity=1.0。
    RSI<30 → buy（超卖反弹信号）。
    """
    return {
        "sma20":    {"sma": close,  "close": close},
        "ema50":    {"ema": close},
        "atr14":    {"atr": atr},
        "adx14":    {"adx": 15.0, "plus_di": 12.0, "minus_di": 18.0},  # ADX<20 → RANGING
        "boll20":   {"bb_upper": close + 30, "bb_lower": close - 30, "bb_mid": close, "close": close},
        "keltner20":{"kc_upper": close + 20, "kc_lower": close - 20},  # KC < BB → 无 Squeeze
        "rsi14":    {"rsi": 22.0},   # RSI<30 → buy（超卖）
    }


# ---------------------------------------------------------------------------
# 测试用 Stub
# ---------------------------------------------------------------------------

class SnapshotSource:
    """最小化的快照源：持有 listener 列表，提供 publish 方法。"""

    def __init__(self, spread_points: float = 5.0, symbol_point: float = 0.01):
        self._listeners: list = []
        self.market_service = type(
            "MS", (),
            {
                "get_current_spread": lambda _s, symbol=None: spread_points,
                "get_symbol_point":   lambda _s, symbol=None: symbol_point,
            },
        )()

    def add_snapshot_listener(self, fn):
        self._listeners.append(fn)

    def remove_snapshot_listener(self, fn):
        self._listeners = [l for l in self._listeners if l is not fn]

    def publish(self, symbol: str, timeframe: str, bar_time: datetime,
                indicators: dict, scope: str = "confirmed") -> None:
        for fn in list(self._listeners):
            fn(symbol, timeframe, bar_time, indicators, scope)


class DummyIndicatorSource:
    def __init__(self, payload: dict = None):
        self._payload = payload or {}

    def get_indicator(self, symbol, timeframe, name):
        return self._payload.get(name)

    def get_all_indicators(self, symbol, timeframe):
        return dict(self._payload)

    def list_indicators(self):
        return [{"name": k} for k in self._payload]


class TradingModuleCapture:
    """捕获所有 dispatch_operation 调用的假 TradingModule。

    内置 threading.Event 通知机制，消除 polling sleep 的 flaky timeout。
    """

    def __init__(self, fail_count: int = 0):
        self.calls: List[tuple] = []
        self._fail_count = fail_count
        self._call_count = 0
        self._call_event = threading.Event()

    def dispatch_operation(self, operation, payload):
        self._call_count += 1
        self.calls.append((operation, payload))
        self._call_event.set()
        if self._fail_count > 0 and self._call_count <= self._fail_count:
            raise RuntimeError("simulated dispatch failure")
        return {"ticket": 123456, "success": True}

    def account_info(self):
        return {"equity": 10000.0}

    def wait_for_calls(self, expected: int, timeout: float = 10.0) -> bool:
        """等待至少 expected 次 dispatch_operation 调用（事件驱动，不 polling）。"""
        deadline = time.monotonic() + timeout
        while len(self.calls) < expected:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._call_event.clear()
            self._call_event.wait(timeout=min(remaining, 1.0))
        return True


# ---------------------------------------------------------------------------
# 辅助构建函数
# ---------------------------------------------------------------------------

def _build_signal_module(indicators: dict = None) -> SignalModule:
    src = DummyIndicatorSource(indicators or {})
    return SignalModule(
        indicator_source=src,
        strategies=[SmaTrendStrategy(), RsiReversionStrategy()],
    )


def _build_runtime(
    signal_module: SignalModule,
    snapshot_source: SnapshotSource,
    *,
    allow_all_sessions: bool = True,
    require_armed: bool = False,
    min_preview_stable_seconds: float = 0.0,
) -> SignalRuntime:
    from src.signals.orchestration.policy import SignalPolicy
    from src.signals.contracts import SESSION_ASIA, SESSION_LONDON, SESSION_NEW_YORK

    sessions = (SESSION_ASIA, SESSION_LONDON, SESSION_NEW_YORK) if allow_all_sessions else (SESSION_LONDON, SESSION_NEW_YORK)
    policy = SignalPolicy(
        allowed_sessions=sessions,
        min_preview_confidence=0.3,
        min_preview_stable_seconds=min_preview_stable_seconds,
        preview_cooldown_seconds=0.0,
        voting_enabled=False,         # 简化：关闭投票引擎，只测单策略
        min_affinity_skip=0.0,        # 不过滤任何策略
    )
    targets = [
        SignalTarget("XAUUSD", "M1", "sma_trend"),
        SignalTarget("XAUUSD", "M1", "rsi_reversion"),
    ]
    return SignalRuntime(
        service=signal_module,
        snapshot_source=snapshot_source,
        targets=targets,
        enable_confirmed_snapshot=True,
        enable_intrabar=True,
        policy=policy,
    )


def _build_executor(
    trading_module,
    *,
    require_armed: bool = False,
    min_confidence: float = 0.3,
    max_consecutive_failures: int = 3,
) -> TradeExecutor:
    from src.trading.execution import ExecutionGate, ExecutionGateConfig

    return TradeExecutor(
        trading_module=trading_module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=min_confidence,
            sl_atr_multiplier=1.5,
            tp_atr_multiplier=3.0,
            max_spread_to_stop_ratio=0.99,   # 测试中不过滤 spread
            max_consecutive_failures=max_consecutive_failures,
            circuit_auto_reset_minutes=0,    # 不自动恢复
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig(require_armed=require_armed)),
        account_balance_getter=lambda: 10000.0,
    )


def _wait_for_calls(module: TradingModuleCapture, expected: int, timeout: float = 10.0) -> bool:
    """向后兼容包装器 — 委托给 TradingModuleCapture.wait_for_calls()。"""
    return module.wait_for_calls(expected, timeout=timeout)


# ===========================================================================
# 测试 1：confirmed snapshot → sma_trend buy → TradeExecutor 下单
# ===========================================================================

def test_confirmed_buy_triggers_trade() -> None:
    """confirmed 快照触发 sma_trend buy 信号，TradeExecutor 成功下单。"""
    indicators = _trending_buy_indicators(close=3000.0, atr=5.0)
    module = _build_signal_module(indicators)
    source = SnapshotSource(spread_points=5.0)
    runtime = _build_runtime(module, source, require_armed=False)

    trading = TradingModuleCapture()
    executor = _build_executor(trading, require_armed=False)
    runtime.add_signal_listener(executor.on_signal_event)
    runtime.start()

    bar_time = datetime.now(timezone.utc) - timedelta(minutes=1)
    source.publish("XAUUSD", "M1", bar_time, indicators, scope="confirmed")

    assert _wait_for_calls(trading, expected=1), "TradeExecutor 未收到下单调用"

    op, payload = trading.calls[0]
    assert op == "trade"
    assert payload["side"] == "buy"
    assert payload["symbol"] == "XAUUSD"
    assert payload["sl"] is not None and payload["sl"] < 3000.0
    assert payload["tp"] is not None and payload["tp"] > 3000.0

    runtime.stop()


# ===========================================================================
# 测试 2：RSI 超卖 → rsi_reversion sell 信号 → 下单
# ===========================================================================

def test_rsi_oversold_triggers_buy_trade() -> None:
    """RSI < 30 触发 rsi_reversion buy 信号，TradeExecutor 成功下单。"""
    indicators = _ranging_rsi_buy_indicators(close=3000.0, atr=4.0)
    module = _build_signal_module(indicators)
    source = SnapshotSource()
    runtime = _build_runtime(module, source, require_armed=False)

    trading = TradingModuleCapture()
    executor = _build_executor(trading, require_armed=False)
    runtime.add_signal_listener(executor.on_signal_event)
    runtime.start()

    bar_time = datetime.now(timezone.utc) - timedelta(minutes=1)
    source.publish("XAUUSD", "M1", bar_time, indicators, scope="confirmed")

    assert _wait_for_calls(trading, expected=1), "TradeExecutor 未收到下单调用"

    op, payload = trading.calls[0]
    assert op == "trade"
    assert payload["side"] == "buy"   # RSI oversold → buy

    runtime.stop()


# ===========================================================================
# 测试 3：require_armed=True 完整状态机流程
#   intrabar(preview) → intrabar(armed) → confirmed → 下单
# ===========================================================================

def test_require_armed_full_state_machine() -> None:
    """require_armed=True 时，信号须经 preview→armed 阶段才能触发下单。

    使用 RsiReversionStrategy（preferred_scopes = ("intrabar", "confirmed")）：
      1. intrabar 快照 (RSI=22 < 30) → preview_buy
      2. intrabar 快照（微小差异绕过去重）→ armed_buy（stable_seconds=0）
      3. confirmed 快照 → confirmed_buy，TradeExecutor 检测到 armed → 下单

    注意：confirmed 队列优先于 intrabar 队列，所以需要等待 intrabar 事件
    全部被消费完，再发布 confirmed 快照，否则 confirmed 会先处理。
    """
    # 使用 RANGING + RSI 超卖指标，触发 RsiReversionStrategy（支持 intrabar）
    indicators = _ranging_rsi_buy_indicators(close=3000.0, atr=5.0)
    module = _build_signal_module(indicators)
    source = SnapshotSource()
    # min_preview_stable_seconds=0 → 第一次 intrabar 后立即进入 armed
    runtime = _build_runtime(
        module, source, require_armed=True, min_preview_stable_seconds=0.0
    )

    trading = TradingModuleCapture()
    executor = _build_executor(trading, require_armed=True)
    runtime.add_signal_listener(executor.on_signal_event)
    runtime.start()

    bar_time = datetime.now(timezone.utc) - timedelta(seconds=30)

    # Step 1: intrabar 快照 → preview_buy（RSI=22 触发 rsi_reversion buy）
    source.publish("XAUUSD", "M1", bar_time, indicators, scope="intrabar")
    # 等待 runtime 消费掉 intrabar 事件（confirmed 队列优先，此时 confirmed 为空）
    time.sleep(0.3)

    # Step 2: 再次 intrabar 快照（方向一致，stable_seconds=0 → 立即升为 armed_buy）
    # 微小差异（rsi 从 22.0 → 22.1）绕过 intrabar 去重
    indicators2 = {**indicators, "rsi14": {"rsi": 22.1}}
    source.publish("XAUUSD", "M1", bar_time, indicators2, scope="intrabar")
    time.sleep(0.3)

    # Step 3: confirmed 快照 → confirmed_buy，TradeExecutor 检测到 preview_state_at_close="armed_buy"
    source.publish("XAUUSD", "M1", bar_time, indicators, scope="confirmed")

    assert _wait_for_calls(trading, expected=1), "require_armed 流程未触发下单"

    op, payload = trading.calls[0]
    assert op == "trade"
    assert payload["side"] == "buy"

    runtime.stop()


# ===========================================================================
# 测试 4：熔断器 — 连续失败后暂停，手动 reset 后恢复
# ===========================================================================

def test_circuit_breaker_pauses_and_resets() -> None:
    """连续失败 max_consecutive_failures 次后熔断，reset 后下一笔成功执行。"""
    indicators = _trending_buy_indicators(close=3000.0, atr=5.0)
    module = _build_signal_module(indicators)
    source = SnapshotSource()
    runtime = _build_runtime(module, source, require_armed=False)

    # 前 3 次失败，第 4 次成功
    trading = TradingModuleCapture(fail_count=3)
    executor = _build_executor(trading, require_armed=False, max_consecutive_failures=3)
    runtime.add_signal_listener(executor.on_signal_event)
    runtime.start()

    # 每次发送不同 bar_time，避免 confirmed 快照去重
    base_time = datetime.now(timezone.utc) - timedelta(minutes=10)

    # 发送 3 个快照（不同 bar_time），触发 3 次失败 → 熔断开路
    for i in range(3):
        bt = base_time + timedelta(minutes=i)
        source.publish("XAUUSD", "M1", bt, indicators, scope="confirmed")
        time.sleep(0.05)

    # 等待 3 次 dispatch 调用完成（含失败的）
    trading.wait_for_calls(3, timeout=10.0)
    executor.flush()

    assert executor._circuit_open, "熔断器应已开路"

    # 再发一个快照（新 bar_time）→ 被熔断器拦截
    before = len(trading.calls)
    bt_block = base_time + timedelta(minutes=3)
    source.publish("XAUUSD", "M1", bt_block, indicators, scope="confirmed")
    time.sleep(0.3)
    executor.flush()
    assert len(trading.calls) == before, "熔断器开路后不应再触发 dispatch"

    # 手动 reset
    executor.reset_circuit()
    assert not executor._circuit_open

    # 再发一个快照（新 bar_time）→ 此次 dispatch 成功（第 4 次，不再 fail）
    bt_reset = base_time + timedelta(minutes=4)
    source.publish("XAUUSD", "M1", bt_reset, indicators, scope="confirmed")
    trading.wait_for_calls(before + 1, timeout=10.0)
    executor.flush()
    assert len(trading.calls) > before, "reset 后应恢复下单"
    _, payload = trading.calls[-1]
    assert payload["side"] == "buy"

    runtime.stop()


# ===========================================================================
# 测试 5：会话过滤 — 非允许时段不评估信号
# ===========================================================================

def test_session_filter_blocks_signal_outside_session() -> None:
    """当 UTC 小时为 off_hours，且 allowed_sessions 不含 off_hours 时，信号被过滤。"""
    from src.signals.execution.filters import SessionFilter

    # UTC 03:00 = 亚盘结束后/欧盘开始前的过渡区（通常属于 off_hours）
    # 这里直接测 SessionFilter 本身的判断
    sf = SessionFilter(allowed_sessions=("london", "new_york"))

    off_hour_utc = datetime(2026, 3, 20, 3, 0, tzinfo=timezone.utc)  # UTC 03:00
    assert not sf.is_active_session(off_hour_utc), "UTC 03:00 不应属于 london/new_york"

    london_hour_utc = datetime(2026, 3, 20, 9, 0, tzinfo=timezone.utc)  # UTC 09:00
    assert sf.is_active_session(london_hour_utc), "UTC 09:00 应属于 london session"


# ===========================================================================
# 测试 6：extract_close_price 工具函数
# ===========================================================================

def testextract_close_price_from_boll_payload() -> None:
    indicators = {
        "boll20": {"bb_upper": 3010.0, "bb_lower": 2990.0, "bb_mid": 3000.0, "close": 3001.5},
        "sma20": {"sma": 2999.0},
    }
    price = extract_close_price(indicators)
    assert price == 3001.5


def testextract_close_price_falls_back_to_bb_mid() -> None:
    """没有 close 字段时，退回到 bb_mid。"""
    indicators = {
        "boll20": {"bb_upper": 3010.0, "bb_lower": 2990.0, "bb_mid": 3000.0},
    }
    price = extract_close_price(indicators)
    assert price == 3000.0


def testextract_close_price_returns_none_if_absent() -> None:
    indicators = {"sma20": {"sma": 3000.0}}
    assert extract_close_price(indicators) is None


# ===========================================================================
# 测试 7：close_position 竞态修复 — order_send=None 且持仓已消失 → 视为成功
# ===========================================================================

def test_close_position_returns_true_when_position_gone_after_send_none() -> None:
    """
    修复验证：当 order_send() 返回 None（经纪商拒单），
    但重查持仓发现 position 已不存在（被 SL/TP 平仓），
    close_position 应返回 True 而不是抛出异常。
    """
    import MetaTrader5 as mt5
    from src.clients.mt5_trading import MT5TradingClient
    from src.config.mt5 import MT5Settings

    # Mock MT5 模块的各函数
    fake_pos = MagicMock()
    fake_pos.symbol = "XAUUSD"
    fake_pos.volume = 0.01
    fake_pos.ticket = 99999
    fake_pos.type = 0  # ORDER_TYPE_BUY
    fake_pos.magic = 0

    fake_tick = MagicMock()
    fake_tick.bid = 3000.0
    fake_tick.ask = 3001.0

    settings = MT5Settings(login=0, password="", server="")
    client = MT5TradingClient(settings=settings)

    with (
        patch.object(client, "connect"),
        patch("MetaTrader5.positions_get") as mock_positions_get,
        patch("MetaTrader5.symbol_info_tick", return_value=fake_tick),
        patch.object(client, "_send_order_with_supported_fillings", return_value=None),
        patch("MetaTrader5.last_error", return_value=(-2, 'Invalid "comment" argument')),
    ):
        # 第一次调用（找持仓）返回 pos，后续调用（竞态重查）返回空 → 已被 SL/TP 平仓
        mock_positions_get.side_effect = [
            (fake_pos,),   # 初始 positions_get → 找到持仓
            (),            # 重查（order_send None 后）→ 持仓已消失
        ]

        result = client.close_position(ticket=99999)
        assert result is True, "持仓已消失时应返回 True，而非抛出异常"


# ===========================================================================
# 测试 8：TradeExecutor 低置信度信号被过滤
# ===========================================================================

def test_trade_executor_skips_low_confidence() -> None:
    trading = TradingModuleCapture()
    executor = _build_executor(trading, min_confidence=0.80)

    event = SignalEvent(
        symbol="XAUUSD", timeframe="M1", strategy="sma_trend",
        direction="buy", confidence=0.50,  # 低于阈值
        signal_state="confirmed_buy", scope="confirmed",
        indicators={"atr14": {"atr": 5.0}, "sma20": {"sma": 3000.0}},
        metadata={"previous_state": "armed_buy", "close_price": 3000.0,
                  "spread_points": 5.0, "spread_price": 0.05, "symbol_point": 0.01},
        generated_at=datetime.now(timezone.utc),
        signal_id="test_low_conf", reason="test",
    )
    executor.on_signal_event(event)
    assert trading.calls == [], "低置信度信号不应触发下单"


# ===========================================================================
# 测试 9：多信号不重复下单（相同 bar_time + 相同策略的 confirmed）
# ===========================================================================

def test_no_duplicate_trade_on_same_bar() -> None:
    """同一 bar_time 的重复 confirmed 快照不应触发重复下单。"""
    indicators = _trending_buy_indicators(close=3000.0, atr=5.0)
    module = _build_signal_module(indicators)
    source = SnapshotSource()
    runtime = _build_runtime(module, source, require_armed=False)

    trading = TradingModuleCapture()
    executor = _build_executor(trading, require_armed=False)
    runtime.add_signal_listener(executor.on_signal_event)
    runtime.start()

    bar_time = datetime.now(timezone.utc) - timedelta(minutes=1)
    # 同一 bar_time 发两次相同快照
    source.publish("XAUUSD", "M1", bar_time, indicators, scope="confirmed")
    source.publish("XAUUSD", "M1", bar_time, indicators, scope="confirmed")

    # 等待第一个快照被处理并触发下单
    assert _wait_for_calls(trading, expected=1), "至少应有 1 次下单调用"
    # 再等一会，确认第 2 次快照不会再触发下单（去重保护）
    time.sleep(0.3)

    # 相同 bar_time + 相同指标的重复 confirmed 快照被状态机去重，只触发 1 次下单
    trade_calls = [c for c in trading.calls if c[0] == "trade"]
    assert len(trade_calls) == 1, f"重复快照触发了 {len(trade_calls)} 次下单，预期只有 1 次"

    runtime.stop()
