"""SignalRuntime warmup 屏障机制单元测试。

测试 warmup_ready_fn 回调、必需指标检查、intrabar 额外屏障。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.signals.models import SignalDecision
from src.signals.orchestration import SignalPolicy, SignalRuntime, SignalTarget


# ── Dummy 对象 ────────────────────────────────────────────────


class DummySnapshotSource:
    def __init__(self) -> None:
        self.snapshot_listeners: list = []

    def add_snapshot_listener(self, listener):  # type: ignore[no-untyped-def]
        self.snapshot_listeners.append(listener)

    def remove_snapshot_listener(self, listener):  # type: ignore[no-untyped-def]
        self.snapshot_listeners = [
            item for item in self.snapshot_listeners if item is not listener
        ]


class DummySignalService:
    def __init__(self) -> None:
        self.evaluate_calls: list = []
        self.persist_calls: list = []

    def strategy_requirements(self, strategy: str):  # type: ignore[no-untyped-def]
        return ("rsi14",)

    def strategy_scopes(self, strategy: str):  # type: ignore[no-untyped-def]
        return ("confirmed", "intrabar")

    def strategy_affinity_map(self, strategy: str):  # type: ignore[no-untyped-def]
        return {}

    def strategy_htf_indicators(self, strategy: str):  # type: ignore[no-untyped-def]
        return {}

    def evaluate(self, **kwargs):  # type: ignore[no-untyped-def]
        self.evaluate_calls.append(kwargs)
        return SignalDecision(
            strategy=kwargs["strategy"],
            symbol=kwargs["symbol"],
            timeframe=kwargs["timeframe"],
            direction="buy",
            confidence=0.8,
            reason="test",
            used_indicators=list(kwargs.get("indicators", {}).keys()),
            metadata={},
        )

    def persist_decision(self, decision, indicators, metadata=None):  # type: ignore[no-untyped-def]
        self.persist_calls.append(
            {"decision": decision, "indicators": indicators, "metadata": metadata or {}}
        )

    def get_strategy(self, name: str):  # type: ignore[no-untyped-def]
        return None

    def recent_signals(self, **kwargs):  # type: ignore[no-untyped-def]
        return []


# ── Helpers ───────────────────────────────────────────────────


_INDICATORS_FULL = {"rsi14": {"rsi": 55}, "atr14": {"atr": 2.5}}
_INDICATORS_NO_ATR = {"rsi14": {"rsi": 55}}


def _make_runtime(
    source: DummySnapshotSource,
    service: DummySignalService,
    warmup_ready_fn=None,  # type: ignore[no-untyped-def]
    targets=None,  # type: ignore[no-untyped-def]
) -> SignalRuntime:
    if targets is None:
        targets = [SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion")]

    policy = SignalPolicy(
        voting_enabled=False,
        warmup_required_indicators=("atr14",),
    )
    return SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=targets,
        enable_confirmed_snapshot=True,
        policy=policy,
        warmup_ready_fn=warmup_ready_fn,
    )


def _bar_time() -> datetime:
    return datetime.now(timezone.utc)


def _emit_and_process(
    rt: SignalRuntime,
    symbol: str,
    timeframe: str,
    indicators: dict,
    scope: str = "confirmed",
) -> bool:
    """调用 _on_snapshot（入队） + process_next_event（出队处理），
    模拟完整的单次快照处理流程。"""
    rt._on_snapshot(symbol, timeframe, _bar_time(), indicators, scope)
    return rt.process_next_event(timeout=0.01)


# ═══════════════════════════════════════════════════════════════
# Warmup 屏障：warmup_ready_fn
# ═══════════════════════════════════════════════════════════════


class TestWarmupBarrier:
    def test_warmup_fn_false_blocks_confirmed(self) -> None:
        """warmup_ready_fn 返回 False 时，confirmed 快照被跳过（不入队）。"""
        source = DummySnapshotSource()
        service = DummySignalService()
        rt = _make_runtime(source, service, warmup_ready_fn=lambda: False)

        rt._on_snapshot("XAUUSD", "M5", _bar_time(), _INDICATORS_FULL, "confirmed")
        # 事件不应入队
        processed = rt.process_next_event(timeout=0.01)
        assert processed is False
        assert len(service.evaluate_calls) == 0
        assert rt.status()["warmup_skipped"] >= 1
        assert rt.status()["warmup_ready"] is False

    def test_warmup_fn_false_blocks_intrabar(self) -> None:
        """warmup_ready_fn 返回 False 时，intrabar 快照也被跳过。"""
        source = DummySnapshotSource()
        service = DummySignalService()
        rt = _make_runtime(source, service, warmup_ready_fn=lambda: False)

        rt._on_snapshot("XAUUSD", "M5", _bar_time(), _INDICATORS_NO_ATR, "intrabar")
        processed = rt.process_next_event(timeout=0.01)
        assert processed is False
        assert len(service.evaluate_calls) == 0

    def test_warmup_fn_true_allows_processing(self) -> None:
        """warmup_ready_fn 返回 True 后，confirmed 快照正常处理。"""
        source = DummySnapshotSource()
        service = DummySignalService()
        rt = _make_runtime(source, service, warmup_ready_fn=lambda: True)

        _emit_and_process(rt, "XAUUSD", "M5", _INDICATORS_FULL, "confirmed")
        assert len(service.evaluate_calls) == 1

    def test_no_warmup_fn_allows_processing(self) -> None:
        """无 warmup_ready_fn 时（standalone 模式），直接放行。"""
        source = DummySnapshotSource()
        service = DummySignalService()
        rt = _make_runtime(source, service, warmup_ready_fn=None)

        _emit_and_process(rt, "XAUUSD", "M5", _INDICATORS_FULL, "confirmed")
        assert len(service.evaluate_calls) == 1
        assert rt.status()["warmup_ready"] is True

    def test_warmup_transition(self) -> None:
        """warmup 从 False→True 后开始处理快照。"""
        source = DummySnapshotSource()
        service = DummySignalService()
        ready = {"value": False}
        rt = _make_runtime(source, service, warmup_ready_fn=lambda: ready["value"])

        # 回补中——跳过
        rt._on_snapshot("XAUUSD", "M5", _bar_time(), _INDICATORS_FULL, "confirmed")
        assert rt.process_next_event(timeout=0.01) is False
        assert len(service.evaluate_calls) == 0

        # 回补完成
        ready["value"] = True
        _emit_and_process(rt, "XAUUSD", "M5", _INDICATORS_FULL, "confirmed")
        assert len(service.evaluate_calls) == 1

    def test_stale_bar_rejected_after_warmup(self) -> None:
        """warmup_ready_fn=True 后，bar_time 过旧的快照仍被跳过（异步管道残留）。

        BackgroundIngestor 标记回补完成后，指标管道中可能仍有排队中的
        历史 bar 快照。这些 stale 快照不应被当作首个实时 bar。
        """
        source = DummySnapshotSource()
        service = DummySignalService()
        rt = _make_runtime(source, service, warmup_ready_fn=lambda: True)

        # M5 bar_time 1 小时前——远超 2×300s+30s 阈值
        stale_time = datetime.now(timezone.utc) - timedelta(hours=1)
        rt._on_snapshot("XAUUSD", "M5", stale_time, _INDICATORS_FULL, "confirmed")
        processed = rt.process_next_event(timeout=0.01)
        assert processed is False
        assert len(service.evaluate_calls) == 0
        # 首个实时 bar 未被标记
        assert rt.status()["warmup_realtime_symbols"] == 0
        assert rt.status()["warmup_skipped"] >= 1

    def test_stale_bar_then_fresh_bar_lifts_barrier(self) -> None:
        """stale bar 被拒绝后，后续 fresh bar 正常解除屏障。"""
        source = DummySnapshotSource()
        service = DummySignalService()
        rt = _make_runtime(source, service, warmup_ready_fn=lambda: True)

        # stale bar 被拒
        stale_time = datetime.now(timezone.utc) - timedelta(hours=1)
        rt._on_snapshot("XAUUSD", "M5", stale_time, _INDICATORS_FULL, "confirmed")
        rt.process_next_event(timeout=0.01)
        assert rt.status()["warmup_realtime_symbols"] == 0

        # fresh bar 通过
        _emit_and_process(rt, "XAUUSD", "M5", _INDICATORS_FULL, "confirmed")
        assert len(service.evaluate_calls) == 1
        assert rt.status()["warmup_realtime_symbols"] == 1

    def test_no_warmup_fn_skips_staleness_check(self) -> None:
        """无 warmup_ready_fn 时不做 staleness 检查（standalone/测试兼容）。"""
        source = DummySnapshotSource()
        service = DummySignalService()
        rt = _make_runtime(source, service, warmup_ready_fn=None)

        old_time = datetime.now(timezone.utc) - timedelta(hours=2)
        rt._on_snapshot("XAUUSD", "M5", old_time, _INDICATORS_FULL, "confirmed")
        processed = rt.process_next_event(timeout=0.01)
        assert processed is True
        assert len(service.evaluate_calls) == 1


# ═══════════════════════════════════════════════════════════════
# 必需指标检查
# ═══════════════════════════════════════════════════════════════


class TestRequiredIndicators:
    def test_missing_atr_skips(self) -> None:
        """confirmed 快照缺少 atr14 时被跳过（warmup 指标完整性检查）。"""
        source = DummySnapshotSource()
        service = DummySignalService()
        rt = _make_runtime(source, service, warmup_ready_fn=lambda: True)

        # 缺少 atr14——不入队
        rt._on_snapshot("XAUUSD", "M5", _bar_time(), _INDICATORS_NO_ATR, "confirmed")
        processed = rt.process_next_event(timeout=0.01)
        assert processed is False
        assert len(service.evaluate_calls) == 0
        assert rt.status()["warmup_skipped"] >= 1

    def test_atr_present_passes(self) -> None:
        """confirmed 快照包含 atr14 时正常评估。"""
        source = DummySnapshotSource()
        service = DummySignalService()
        rt = _make_runtime(source, service, warmup_ready_fn=lambda: True)

        _emit_and_process(rt, "XAUUSD", "M5", _INDICATORS_FULL, "confirmed")
        assert len(service.evaluate_calls) == 1


# ═══════════════════════════════════════════════════════════════
# Intrabar 额外屏障
# ═══════════════════════════════════════════════════════════════


class TestIntrabarBarrier:
    def test_intrabar_blocked_before_first_confirmed(self) -> None:
        """回补完成后，confirmed 首个实时 bar 未到达前，intrabar 仍被跳过。"""
        source = DummySnapshotSource()
        service = DummySignalService()
        rt = _make_runtime(source, service, warmup_ready_fn=lambda: True)

        # 先发 intrabar——应被跳过（首个 confirmed bar 尚未到达）
        rt._on_snapshot("XAUUSD", "M5", _bar_time(), _INDICATORS_NO_ATR, "intrabar")
        processed = rt.process_next_event(timeout=0.01)
        assert processed is False
        assert len(service.evaluate_calls) == 0

    def test_intrabar_allowed_after_first_confirmed(self) -> None:
        """confirmed 首个实时 bar 到达后，intrabar 正常处理。"""
        source = DummySnapshotSource()
        service = DummySignalService()
        rt = _make_runtime(source, service, warmup_ready_fn=lambda: True)

        # 先发 confirmed bar，解除屏障
        _emit_and_process(rt, "XAUUSD", "M5", _INDICATORS_FULL, "confirmed")
        eval_count_after_confirmed = len(service.evaluate_calls)

        # 再发 intrabar——应被处理
        _emit_and_process(rt, "XAUUSD", "M5", _INDICATORS_NO_ATR, "intrabar")
        assert len(service.evaluate_calls) > eval_count_after_confirmed

    def test_intrabar_different_symbol_still_blocked(self) -> None:
        """(symbol, tf) 粒度独立：XAUUSD confirmed 不解除 EURUSD 的 intrabar 屏障。"""
        source = DummySnapshotSource()
        service = DummySignalService()
        targets = [
            SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion"),
            SignalTarget(symbol="EURUSD", timeframe="M5", strategy="rsi_reversion"),
        ]
        rt = _make_runtime(source, service, warmup_ready_fn=lambda: True, targets=targets)

        # XAUUSD confirmed——解除 XAUUSD 屏障
        _emit_and_process(rt, "XAUUSD", "M5", _INDICATORS_FULL, "confirmed")

        # EURUSD intrabar——EURUSD 尚未收到 confirmed，应被跳过
        before = len(service.evaluate_calls)
        rt._on_snapshot("EURUSD", "M5", _bar_time(), _INDICATORS_NO_ATR, "intrabar")
        processed = rt.process_next_event(timeout=0.01)
        assert processed is False
        assert len(service.evaluate_calls) == before

    def test_stale_confirmed_does_not_lift_intrabar_barrier(self) -> None:
        """stale confirmed bar 被拒后，intrabar 屏障仍然有效。"""
        source = DummySnapshotSource()
        service = DummySignalService()
        rt = _make_runtime(source, service, warmup_ready_fn=lambda: True)

        # stale confirmed——被拒，不解除屏障
        stale_time = datetime.now(timezone.utc) - timedelta(hours=1)
        rt._on_snapshot("XAUUSD", "M5", stale_time, _INDICATORS_FULL, "confirmed")
        rt.process_next_event(timeout=0.01)

        # intrabar 仍被阻塞（首个实时 confirmed bar 未到达）
        rt._on_snapshot("XAUUSD", "M5", _bar_time(), _INDICATORS_NO_ATR, "intrabar")
        processed = rt.process_next_event(timeout=0.01)
        assert processed is False
        assert len(service.evaluate_calls) == 0

    def test_no_warmup_fn_intrabar_not_blocked(self) -> None:
        """无 warmup_ready_fn 时 intrabar 不受额外屏障限制。"""
        source = DummySnapshotSource()
        service = DummySignalService()
        rt = _make_runtime(source, service, warmup_ready_fn=None)

        _emit_and_process(rt, "XAUUSD", "M5", _INDICATORS_NO_ATR, "intrabar")
        # 无 warmup_fn → 无额外屏障
        assert len(service.evaluate_calls) >= 1


# ═══════════════════════════════════════════════════════════════
# Status 输出
# ═══════════════════════════════════════════════════════════════


class TestWarmupStatus:
    def test_status_warmup_fields_present(self) -> None:
        source = DummySnapshotSource()
        service = DummySignalService()
        rt = _make_runtime(source, service, warmup_ready_fn=lambda: True)
        status = rt.status()
        assert "warmup_skipped" in status
        assert "warmup_ready" in status
        assert "warmup_realtime_symbols" in status

    def test_status_warmup_realtime_symbols_increments(self) -> None:
        source = DummySnapshotSource()
        service = DummySignalService()
        targets = [
            SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion"),
            SignalTarget(symbol="XAUUSD", timeframe="M15", strategy="rsi_reversion"),
        ]
        rt = _make_runtime(source, service, warmup_ready_fn=lambda: True, targets=targets)

        assert rt.status()["warmup_realtime_symbols"] == 0

        _emit_and_process(rt, "XAUUSD", "M5", _INDICATORS_FULL, "confirmed")
        assert rt.status()["warmup_realtime_symbols"] == 1

        _emit_and_process(rt, "XAUUSD", "M15", _INDICATORS_FULL, "confirmed")
        assert rt.status()["warmup_realtime_symbols"] == 2
