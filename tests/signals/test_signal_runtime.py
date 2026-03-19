from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.signals.models import SignalDecision
from src.signals.policy import SignalPolicy
from src.signals.runtime import SignalRuntime, SignalTarget


class DummySnapshotSource:
    def __init__(self):
        self.snapshot_listeners = []

    def add_snapshot_listener(self, listener):
        self.snapshot_listeners.append(listener)

    def remove_snapshot_listener(self, listener):
        self.snapshot_listeners = [
            item for item in self.snapshot_listeners if item is not listener
        ]

    def publish(
        self,
        symbol: str,
        timeframe: str,
        bar_time: datetime,
        indicators: dict,
        scope: str = "confirmed",
    ):
        for listener in list(self.snapshot_listeners):
            listener(symbol, timeframe, bar_time, indicators, scope)


class DummySignalService:
    def __init__(self):
        self.evaluate_calls = []
        self.persist_calls = []
        self.recent_rows = []

    def strategy_requirements(self, strategy: str):
        mapping = {
            "sma_trend": ("sma20", "ema50"),
            "rsi_reversion": ("rsi14",),
        }
        return mapping.get(strategy, ())

    def strategy_scopes(self, strategy: str):
        return ("confirmed", "intrabar")

    def strategy_affinity_map(self, strategy: str):
        return {}

    def evaluate(self, **kwargs):
        self.evaluate_calls.append(kwargs)
        indicators = kwargs["indicators"]
        strategy = kwargs["strategy"]
        if strategy == "sma_trend":
            action = (
                "buy"
                if indicators["sma20"]["sma"] > indicators["ema50"]["ema"]
                else "sell"
            )
            confidence = 0.8
        else:
            rsi = indicators["rsi14"]["rsi"]
            action = "buy" if rsi <= 30 else "sell" if rsi >= 70 else "hold"
            confidence = 0.9 if action != "hold" else 0.2
        return SignalDecision(
            strategy=strategy,
            symbol=kwargs["symbol"],
            timeframe=kwargs["timeframe"],
            action=action,
            confidence=confidence,
            reason="test",
            used_indicators=list(indicators.keys()),
            metadata={},
        )

    def persist_decision(self, decision, indicators, metadata=None):
        self.persist_calls.append(
            {
                "decision": decision,
                "indicators": indicators,
                "metadata": metadata or {},
            }
        )

    def recent_signals(self, **kwargs):
        scope = kwargs.get("scope", "confirmed")
        rows = list(self.recent_rows)
        if scope == "all":
            return rows
        return [row for row in rows if row.get("scope", "confirmed") == scope]


def test_signal_runtime_processes_confirmed_snapshot_event() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="sma_trend")],
        enable_confirmed_snapshot=True,
        enable_intrabar=False,
    )

    runtime._on_snapshot(
        "XAUUSD",
        "M5",
        datetime.now(timezone.utc),
        {"sma20": {"sma": 201.0}, "ema50": {"ema": 200.0}},
        "confirmed",
    )
    processed = runtime.process_next_event(timeout=0.01)

    assert processed is True
    assert len(service.evaluate_calls) == 1
    assert service.evaluate_calls[0]["strategy"] == "sma_trend"
    assert service.evaluate_calls[0]["indicators"]["sma20"]["sma"] == 201.0
    assert service.evaluate_calls[0]["metadata"]["scope"] == "confirmed"
    assert service.evaluate_calls[0]["persist"] is False
    assert service.persist_calls[0]["metadata"]["signal_state"] == "confirmed_buy"


def test_signal_runtime_status_exposes_trigger_mode() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="sma_trend")],
        enable_confirmed_snapshot=True,
        enable_intrabar=False,
    )

    status = runtime.status()

    assert status["target_count"] == 1
    assert status["trigger_mode"]["confirmed_snapshot"] is True
    assert status["trigger_mode"]["intrabar"] is False
    assert "confirmed_backpressure_waits" in status
    assert "confirmed_backpressure_failures" in status


def test_signal_runtime_processes_intrabar_snapshot_when_enabled() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[
            SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion")
        ],
        enable_confirmed_snapshot=True,
        enable_intrabar=True,
        policy=SignalPolicy(
            min_preview_confidence=0.5,
            min_preview_bar_progress=0.0,
            min_preview_stable_seconds=0.0,
            preview_cooldown_seconds=0.0,
        ),
    )

    runtime._on_snapshot(
        "XAUUSD",
        "M5",
        datetime.now(timezone.utc) - timedelta(seconds=120),
        {"rsi14": {"rsi": 24.0}},
        "intrabar",
    )
    processed = runtime.process_next_event(timeout=0.01)

    assert processed is True
    assert service.evaluate_calls[0]["strategy"] == "rsi_reversion"
    assert service.evaluate_calls[0]["metadata"]["scope"] == "intrabar"
    assert "signal_trace_id" in service.evaluate_calls[0]["metadata"]
    assert "bar_progress" in service.evaluate_calls[0]["metadata"]
    assert service.persist_calls[0]["metadata"]["signal_state"] == "preview_buy"


def test_signal_runtime_promotes_preview_signal_to_armed_after_stable_window() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[
            SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion")
        ],
        enable_confirmed_snapshot=True,
        enable_intrabar=True,
        policy=SignalPolicy(
            min_preview_confidence=0.5,
            min_preview_bar_progress=0.0,
            min_preview_stable_seconds=5.0,
            preview_cooldown_seconds=0.0,
        ),
    )

    bar_time = datetime.now(timezone.utc) - timedelta(seconds=180)
    runtime._enqueue(
        (
            "intrabar",
            "XAUUSD",
            "M5",
            {"rsi14": {"rsi": 24.0}},
            {
                "scope": "intrabar",
                "bar_time": bar_time.isoformat(),
                "snapshot_time": (
                    datetime.now(timezone.utc) - timedelta(seconds=10)
                ).isoformat(),
                "trigger_source": "intrabar_snapshot",
                "bar_progress": 0.5,
            },
        )
    )
    runtime.process_next_event(timeout=0.01)
    runtime._enqueue(
        (
            "intrabar",
            "XAUUSD",
            "M5",
            {"rsi14": {"rsi": 24.0}},
            {
                "scope": "intrabar",
                "bar_time": bar_time.isoformat(),
                "snapshot_time": datetime.now(timezone.utc).isoformat(),
                "trigger_source": "intrabar_snapshot",
                "bar_progress": 0.55,
            },
        )
    )
    runtime.process_next_event(timeout=0.01)

    assert len(service.persist_calls) == 2
    assert service.persist_calls[0]["metadata"]["signal_state"] == "preview_buy"
    assert service.persist_calls[1]["metadata"]["signal_state"] == "armed_buy"


def test_signal_runtime_emits_confirmed_cancelled_when_bias_returns_to_idle() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[
            SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion")
        ],
        enable_confirmed_snapshot=True,
        enable_intrabar=False,
    )

    runtime._enqueue(
        (
            "confirmed",
            "XAUUSD",
            "M5",
            {"rsi14": {"rsi": 20.0}},
            {
                "scope": "confirmed",
                "bar_time": (
                    datetime.now(timezone.utc) - timedelta(minutes=5)
                ).isoformat(),
                "snapshot_time": (
                    datetime.now(timezone.utc) - timedelta(minutes=5)
                ).isoformat(),
                "trigger_source": "confirmed_snapshot",
            },
        )
    )
    runtime.process_next_event(timeout=0.01)
    runtime._enqueue(
        (
            "confirmed",
            "XAUUSD",
            "M5",
            {"rsi14": {"rsi": 50.0}},
            {
                "scope": "confirmed",
                "bar_time": datetime.now(timezone.utc).isoformat(),
                "snapshot_time": datetime.now(timezone.utc).isoformat(),
                "trigger_source": "confirmed_snapshot",
            },
        )
    )
    runtime.process_next_event(timeout=0.01)

    assert service.persist_calls[0]["metadata"]["signal_state"] == "confirmed_buy"
    assert service.persist_calls[1]["metadata"]["signal_state"] == "confirmed_cancelled"


def test_signal_runtime_restores_recent_confirmed_state() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    bar_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    service.recent_rows = [
        {
            "generated_at": bar_time.isoformat(),
            "signal_id": "restored-1",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "strategy": "rsi_reversion",
            "action": "buy",
            "confidence": 0.9,
            "reason": "restored",
            "scope": "confirmed",
            "used_indicators": ["rsi14"],
            "indicators_snapshot": {"rsi14": {"rsi": 20.0}},
            "metadata": {
                "scope": "confirmed",
                "bar_time": bar_time.isoformat(),
                "signal_state": "confirmed_buy",
            },
        }
    ]
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[
            SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion")
        ],
        enable_confirmed_snapshot=True,
        enable_intrabar=False,
    )

    runtime._restore_state()

    state = runtime._state_by_target[("XAUUSD", "M5", "rsi_reversion")]
    assert state.confirmed_state == "confirmed_buy"


def test_signal_runtime_skips_strategies_when_required_indicators_missing() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[
            SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="sma_trend"),
            SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion"),
        ],
        enable_confirmed_snapshot=True,
        enable_intrabar=True,
        policy=SignalPolicy(
            min_preview_confidence=0.5,
            min_preview_bar_progress=0.0,
            min_preview_stable_seconds=0.0,
            preview_cooldown_seconds=0.0,
        ),
    )

    runtime._on_snapshot(
        "XAUUSD",
        "M5",
        datetime.now(timezone.utc) - timedelta(seconds=120),
        {"rsi14": {"rsi": 24.0}},
        "intrabar",
    )
    runtime.process_next_event(timeout=0.01)

    assert len(service.evaluate_calls) == 1
    assert service.evaluate_calls[0]["strategy"] == "rsi_reversion"


def test_signal_runtime_deduplicates_same_required_indicator_snapshot_for_same_bar() -> (
    None
):
    source = DummySnapshotSource()
    service = DummySignalService()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[
            SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion")
        ],
        enable_confirmed_snapshot=True,
        enable_intrabar=True,
        policy=SignalPolicy(
            min_preview_confidence=0.5,
            min_preview_bar_progress=0.0,
            min_preview_stable_seconds=0.0,
            preview_cooldown_seconds=0.0,
        ),
    )

    bar_time = datetime.now(timezone.utc) - timedelta(seconds=120)
    runtime._on_snapshot(
        "XAUUSD",
        "M5",
        bar_time,
        {"rsi14": {"rsi": 24.0}},
        "intrabar",
    )
    runtime.process_next_event(timeout=0.01)
    runtime._on_snapshot(
        "XAUUSD",
        "M5",
        bar_time,
        {"rsi14": {"rsi": 24.0}, "sma20": {"sma": 200.0}, "ema50": {"ema": 199.5}},
        "intrabar",
    )
    runtime.process_next_event(timeout=0.01)

    assert len(service.evaluate_calls) == 1
    assert len(service.persist_calls) == 1


def test_signal_runtime_confirmed_queue_is_drained_before_intrabar() -> None:
    """Confirmed events must be processed before intrabar events even when both are queued."""
    source = DummySnapshotSource()
    service = DummySignalService()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[
            SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion")
        ],
        enable_confirmed_snapshot=True,
        enable_intrabar=True,
        policy=SignalPolicy(
            min_preview_confidence=0.5,
            min_preview_bar_progress=0.0,
            min_preview_stable_seconds=0.0,
            preview_cooldown_seconds=0.0,
        ),
    )

    # Enqueue intrabar first, then confirmed.
    bar_time = datetime.now(timezone.utc) - timedelta(seconds=120)
    runtime._enqueue(
        (
            "intrabar",
            "XAUUSD",
            "M5",
            {"rsi14": {"rsi": 24.0}},
            {
                "scope": "intrabar",
                "bar_time": bar_time.isoformat(),
                "snapshot_time": datetime.now(timezone.utc).isoformat(),
                "trigger_source": "intrabar_snapshot",
                "bar_progress": 0.5,
            },
        )
    )
    runtime._enqueue(
        (
            "confirmed",
            "XAUUSD",
            "M5",
            {"rsi14": {"rsi": 24.0}},
            {
                "scope": "confirmed",
                "bar_time": bar_time.isoformat(),
                "snapshot_time": datetime.now(timezone.utc).isoformat(),
                "trigger_source": "confirmed_snapshot",
            },
        )
    )

    # First call must process the confirmed event despite it being enqueued second.
    runtime.process_next_event(timeout=0.01)
    assert len(service.evaluate_calls) == 1
    assert service.evaluate_calls[0]["metadata"]["scope"] == "confirmed"

    # Second call processes the intrabar event.
    runtime.process_next_event(timeout=0.01)
    assert len(service.evaluate_calls) == 2
    assert service.evaluate_calls[1]["metadata"]["scope"] == "intrabar"


def test_signal_runtime_status_exposes_split_queues() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[
            SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion")
        ],
        enable_confirmed_snapshot=True,
        enable_intrabar=True,
    )

    status = runtime.status()

    assert "confirmed_queue_size" in status
    assert "confirmed_queue_capacity" in status
    assert "intrabar_queue_size" in status
    assert "intrabar_queue_capacity" in status
    assert status["confirmed_queue_capacity"] == 512
    assert status["intrabar_queue_capacity"] == 4096
    assert "dropped_confirmed" in status
    assert "dropped_intrabar" in status
