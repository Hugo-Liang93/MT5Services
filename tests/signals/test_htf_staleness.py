"""HTF 指标 staleness 检查测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.signals.orchestration.policy import SignalPolicy
from src.signals.orchestration.runtime import SignalRuntime, SignalTarget


class DummySnapshotSource:
    def __init__(self, indicator_data=None):
        self.snapshot_listeners: list = []
        self._indicator_data = indicator_data or {}
        self.current_trace_id: str | None = None

    def add_snapshot_listener(self, listener):
        self.snapshot_listeners.append(listener)

    def remove_snapshot_listener(self, listener):
        self.snapshot_listeners = [
            item for item in self.snapshot_listeners if item is not listener
        ]

    def get_current_trace_id(self) -> str | None:
        return self.current_trace_id

    def get_indicator(self, symbol, timeframe, indicator_name):
        return self._indicator_data.get((symbol, timeframe, indicator_name))


def _make_runtime(source=None, htf_target_config=None):
    from src.signals.models import SignalDecision

    class DummyService:
        def __init__(self):
            self.evaluate_calls = []

        def strategy_capability_catalog(self):
            return [
                {
                    "name": "strat_a",
                    "valid_scopes": ["confirmed"],
                    "needed_indicators": ["ema50"],
                    "needs_intrabar": False,
                    "needs_htf": True,
                    "regime_affinity": {},
                    "htf_requirements": {"adx14": "H1"},
                }
            ]

        def strategy_requirements(self, strategy):
            return ("ema50",)

        def strategy_scopes(self, strategy):
            return ("confirmed",)

        def strategy_affinity_map(self, strategy):
            return {}

        def evaluate(self, **kwargs):
            self.evaluate_calls.append(kwargs)
            return SignalDecision(
                strategy=kwargs["strategy"],
                symbol=kwargs["symbol"],
                timeframe=kwargs["timeframe"],
                direction="hold",
                confidence=0.3,
                reason="test",
            )

        def persist_decision(self, decision, indicators, metadata=None):
            pass

        soft_regime_enabled = False

    service = DummyService()
    source = source or DummySnapshotSource()
    targets = [
        SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="strat_a"),
        SignalTarget(symbol="XAUUSD", timeframe="H1", strategy="strat_a"),
    ]
    if htf_target_config is None:
        htf_target_config = {"strat_a.adx14": "H1"}
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=targets,
        enable_confirmed_snapshot=True,
        policy=SignalPolicy(),
        htf_indicators_enabled=True,
        htf_target_config=htf_target_config,
    )
    return runtime, service


class TestHTFStaleness:

    def test_fresh_data_is_injected(self):
        """bar_time 在 2×H1 周期内 → 正常注入。"""
        now = datetime.now(timezone.utc)
        fresh_time = (now - timedelta(minutes=30)).isoformat()
        source = DummySnapshotSource(
            indicator_data={
                ("XAUUSD", "H1", "adx14"): {
                    "adx": 25.0,
                    "_bar_time": fresh_time,
                },
            }
        )
        runtime, _ = _make_runtime(source=source)
        result = runtime._resolve_htf_indicators(
            "XAUUSD", "M5", {"H1": ["adx14"]}
        )
        assert "H1" in result
        assert result["H1"]["adx14"]["adx"] == 25.0

    def test_stale_data_is_skipped(self):
        """bar_time 超过 2×H1 周期(7200s) → 跳过注入。"""
        now = datetime.now(timezone.utc)
        stale_time = (now - timedelta(hours=3)).isoformat()
        source = DummySnapshotSource(
            indicator_data={
                ("XAUUSD", "H1", "adx14"): {
                    "adx": 25.0,
                    "_bar_time": stale_time,
                },
            }
        )
        runtime, _ = _make_runtime(source=source)
        result = runtime._resolve_htf_indicators(
            "XAUUSD", "M5", {"H1": ["adx14"]}
        )
        assert result == {}

    def test_no_bar_time_still_injected(self):
        """无 _bar_time 字段 → 正常注入（不做 staleness 检查）。"""
        source = DummySnapshotSource(
            indicator_data={
                ("XAUUSD", "H1", "adx14"): {"adx": 25.0},
            }
        )
        runtime, _ = _make_runtime(source=source)
        result = runtime._resolve_htf_indicators(
            "XAUUSD", "M5", {"H1": ["adx14"]}
        )
        assert "H1" in result
        assert result["H1"]["adx14"]["adx"] == 25.0
