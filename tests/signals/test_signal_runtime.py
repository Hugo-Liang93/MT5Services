from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.signals.evaluation.regime import RegimeType
from src.signals.execution.filters import SessionFilter, SignalFilterChain
from src.signals.models import SignalDecision
from src.signals.orchestration import SignalPolicy, SignalRuntime, SignalTarget


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


class DummySpreadSource(DummySnapshotSource):
    def __init__(self, spread_points: float, symbol_point: float = 0.01):
        super().__init__()
        self.market_service = type(
            "MarketServiceStub",
            (),
            {
                "get_current_spread": lambda _self, symbol=None: spread_points,
                "get_symbol_point": lambda _self, symbol=None: symbol_point,
            },
        )()


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

    def strategy_htf_indicators(self, strategy: str):
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


class DummyStructureAnalyzer:
    def __init__(self, enabled: bool = True):
        self.config = SimpleNamespace(
            enabled=enabled,
            lookback_bars=400,
            m1_lookback_bars=120,
        )
        self.calls = []
        self._cache: dict[tuple[str, str], dict] = {}

    @property
    def cache_entries(self) -> int:
        return len(self._cache)

    def analyze(
        self,
        symbol,
        timeframe,
        *,
        event_time=None,
        latest_close=None,
        lookback_bars_override=None,
    ):
        self.calls.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "event_time": event_time,
                "latest_close": latest_close,
                "lookback_bars_override": lookback_bars_override,
            }
        )
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "structure_bias": "bullish_breakout",
            "breakout_state": "above_previous_day_high",
            "close_price": latest_close,
        }

    def analyze_cached(
        self,
        symbol,
        timeframe,
        *,
        scope="confirmed",
        event_time=None,
        latest_close=None,
        lookback_bars_override=None,
    ):
        cache_key = (symbol, timeframe)
        if scope == "intrabar" and cache_key in self._cache:
            return dict(self._cache[cache_key])
        result = self.analyze(
            symbol,
            timeframe,
            event_time=event_time,
            latest_close=latest_close,
            lookback_bars_override=lookback_bars_override,
        )
        if scope == "confirmed" and result:
            self._cache[cache_key] = dict(result)
        return result


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
        {"sma20": {"sma": 201.0}, "ema50": {"ema": 200.0}, "atr14": {"atr": 5.0}},
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


def test_signal_runtime_fuses_intrabar_and_confirmed_votes_per_strategy() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion")],
        enable_confirmed_snapshot=True,
        enable_intrabar=True,
    )

    intrabar_decision = SignalDecision(
        strategy="rsi_reversion",
        symbol="XAUUSD",
        timeframe="M5",
        action="buy",
        confidence=0.6,
        reason="intrabar",
    )
    confirmed_decision = SignalDecision(
        strategy="rsi_reversion",
        symbol="XAUUSD",
        timeframe="M5",
        action="buy",
        confidence=0.9,
        reason="confirmed",
    )
    bar_time = datetime.now(timezone.utc)

    first = runtime._fuse_vote_decisions(
        "XAUUSD", "M5", bar_time, "intrabar", [intrabar_decision]
    )
    second = runtime._fuse_vote_decisions(
        "XAUUSD", "M5", bar_time, "confirmed", [confirmed_decision]
    )

    assert len(first) == 1
    assert len(second) == 1
    assert second[0].reason == "confirmed"


def test_signal_runtime_injects_soft_regime_metadata_when_enabled() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    service.soft_regime_enabled = True
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
        {
            "adx14": {"adx": 20.5},
            "sma20": {"sma": 201.0},
            "ema50": {"ema": 200.0},
            "atr14": {"atr": 5.0},
        },
        "confirmed",
    )
    runtime.process_next_event(timeout=0.01)

    metadata = service.evaluate_calls[0]["metadata"]
    assert "_soft_regime" in metadata
    assert set(metadata["regime_probabilities"]) == {
        "trending",
        "ranging",
        "breakout",
        "uncertain",
    }


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
    assert status["confirmed_queue_capacity"] == 4096
    assert status["intrabar_queue_capacity"] == 8192
    assert "dropped_confirmed" in status
    assert "dropped_intrabar" in status


def test_signal_runtime_injects_spread_points_from_market_service() -> None:
    source = DummySpreadSource(22.0)
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
        {"sma20": {"sma": 201.0}, "ema50": {"ema": 200.0}, "atr14": {"atr": 5.0}},
        "confirmed",
    )
    runtime.process_next_event(timeout=0.01)

    assert service.evaluate_calls[0]["metadata"]["spread_points"] == 22.0
    assert service.evaluate_calls[0]["metadata"]["symbol_point"] == 0.01
    assert service.evaluate_calls[0]["metadata"]["spread_price"] == 0.22


def test_signal_runtime_status_reflects_market_structure_config_enabled() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="sma_trend")],
        enable_confirmed_snapshot=True,
        enable_intrabar=False,
        market_structure_analyzer=DummyStructureAnalyzer(enabled=False),
    )

    status = runtime.status()

    assert status["market_structure_enabled"] is False


def test_signal_runtime_skips_strategy_outside_strategy_sessions() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion")],
        enable_confirmed_snapshot=True,
        enable_intrabar=False,
        policy=SignalPolicy(strategy_sessions={"rsi_reversion": ("asia",)}),
    )

    runtime._enqueue(
        (
            "confirmed",
            "XAUUSD",
            "M5",
            {"rsi14": {"rsi": 24.0}},
            {
                "scope": "confirmed",
                "bar_time": "2026-03-19T10:00:00+00:00",
                "snapshot_time": "2026-03-19T10:00:00+00:00",
                "trigger_source": "confirmed_snapshot",
            },
        )
    )
    runtime.process_next_event(timeout=0.01)

    assert service.evaluate_calls == []


def test_signal_runtime_injects_market_structure_context() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    analyzer = DummyStructureAnalyzer()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="sma_trend")],
        enable_confirmed_snapshot=True,
        enable_intrabar=False,
        market_structure_analyzer=analyzer,
    )

    runtime._on_snapshot(
        "XAUUSD",
        "M5",
        datetime.now(timezone.utc),
        {"sma20": {"sma": 201.0}, "ema50": {"ema": 200.0}, "atr14": {"atr": 5.0}},
        "confirmed",
    )
    runtime.process_next_event(timeout=0.01)

    structure = service.evaluate_calls[0]["metadata"]["market_structure"]
    assert structure["structure_bias"] == "bullish_breakout"
    assert structure["breakout_state"] == "above_previous_day_high"
    assert analyzer.calls[0]["event_time"].isoformat() == service.evaluate_calls[0]["metadata"]["bar_time"]


def test_signal_runtime_uses_bar_time_for_confirmed_session_context() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    analyzer = DummyStructureAnalyzer()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion")],
        enable_confirmed_snapshot=True,
        enable_intrabar=False,
        policy=SignalPolicy(strategy_sessions={"rsi_reversion": ("asia",)}),
        filter_chain=SignalFilterChain(session_filter=SessionFilter(("asia", "london", "new_york"))),
        market_structure_analyzer=analyzer,
    )

    runtime._enqueue(
        (
            "confirmed",
            "XAUUSD",
            "M5",
            {"rsi14": {"rsi": 24.0}},
            {
                "scope": "confirmed",
                "bar_time": "2026-03-19T01:00:00+00:00",
                "snapshot_time": "2026-03-19T10:00:00+00:00",
                "trigger_source": "confirmed_snapshot",
            },
        )
    )

    runtime.process_next_event(timeout=0.01)

    assert len(service.evaluate_calls) == 1
    assert service.evaluate_calls[0]["metadata"]["session_buckets"] == ["asia"]
    assert analyzer.calls[0]["event_time"] == datetime(2026, 3, 19, 1, 0, tzinfo=timezone.utc)


def test_signal_runtime_reuses_cached_market_structure_for_intrabar() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    analyzer = DummyStructureAnalyzer()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="sma_trend")],
        enable_confirmed_snapshot=True,
        enable_intrabar=True,
        market_structure_analyzer=analyzer,
    )
    bar_time = datetime(2026, 3, 19, 10, 0, tzinfo=timezone.utc)

    runtime._enqueue(
        (
            "confirmed",
            "XAUUSD",
            "M5",
            {"sma20": {"sma": 201.0}, "ema50": {"ema": 200.0}},
            {
                "scope": "confirmed",
                "bar_time": bar_time.isoformat(),
                "snapshot_time": bar_time.isoformat(),
                "trigger_source": "confirmed_snapshot",
            },
        )
    )
    runtime.process_next_event(timeout=0.01)

    runtime._enqueue(
        (
            "intrabar",
            "XAUUSD",
            "M5",
            {"sma20": {"sma": 202.0}, "ema50": {"ema": 200.0}},
            {
                "scope": "intrabar",
                "bar_time": bar_time.isoformat(),
                "snapshot_time": datetime(2026, 3, 19, 10, 1, tzinfo=timezone.utc).isoformat(),
                "trigger_source": "intrabar_snapshot",
                "bar_progress": 0.2,
            },
        )
    )
    runtime.process_next_event(timeout=0.01)

    assert len(analyzer.calls) == 1
    assert runtime.status()["market_structure_cache_entries"] == 1
    assert (
        service.evaluate_calls[1]["metadata"]["market_structure"]["structure_bias"]
        == "bullish_breakout"
    )


def test_signal_runtime_uses_shorter_m1_market_structure_lookback() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    analyzer = DummyStructureAnalyzer()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M1", strategy="sma_trend")],
        enable_confirmed_snapshot=True,
        enable_intrabar=False,
        market_structure_analyzer=analyzer,
    )

    runtime._on_snapshot(
        "XAUUSD",
        "M1",
        datetime.now(timezone.utc),
        {"sma20": {"sma": 201.0}, "ema50": {"ema": 200.0}, "atr14": {"atr": 5.0}},
        "confirmed",
    )
    runtime.process_next_event(timeout=0.01)

    assert analyzer.calls[0]["lookback_bars_override"] == 120


def test_signal_runtime_skips_market_structure_when_all_strategies_filtered_by_affinity() -> None:
    """当所有策略的 affinity 都低于 min_affinity_skip 时，跳过市场结构分析。"""
    source = DummySnapshotSource()
    service = DummySignalService()
    analyzer = DummyStructureAnalyzer()

    # 给 strategy_affinity_map 返回极低的 affinity
    service.strategy_affinity_map = lambda strategy: {
        RegimeType.TRENDING: 0.05,
        RegimeType.RANGING: 0.05,
        RegimeType.BREAKOUT: 0.05,
        RegimeType.UNCERTAIN: 0.05,
    }

    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="sma_trend")],
        enable_confirmed_snapshot=True,
        enable_intrabar=False,
        market_structure_analyzer=analyzer,
        policy=SignalPolicy(min_affinity_skip=0.15),
    )

    runtime._on_snapshot(
        "XAUUSD",
        "M5",
        datetime.now(timezone.utc),
        {"sma20": {"sma": 201.0}, "ema50": {"ema": 200.0}, "atr14": {"atr": 5.0}},
        "confirmed",
    )
    runtime.process_next_event(timeout=0.01)

    # 市场结构分析不应被调用（所有策略 affinity < 0.15 → 全部跳过）
    assert analyzer.calls == []
    # 策略评估也不应发生
    assert service.evaluate_calls == []


def test_signal_runtime_defers_market_structure_until_strategy_passes_gate() -> None:
    """市场结构分析延迟到第一个通过 affinity gate 的策略时才计算。"""
    source = DummySnapshotSource()
    service = DummySignalService()
    analyzer = DummyStructureAnalyzer()

    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="sma_trend")],
        enable_confirmed_snapshot=True,
        enable_intrabar=False,
        market_structure_analyzer=analyzer,
        policy=SignalPolicy(min_affinity_skip=0.0),
    )

    runtime._on_snapshot(
        "XAUUSD",
        "M5",
        datetime.now(timezone.utc),
        {"sma20": {"sma": 201.0}, "ema50": {"ema": 200.0}, "atr14": {"atr": 5.0}},
        "confirmed",
    )
    runtime.process_next_event(timeout=0.01)

    # 策略通过了评估，市场结构应被调用（延迟计算生效）
    assert len(analyzer.calls) == 1
    # 市场结构数据应注入到策略的 metadata 中
    assert service.evaluate_calls[0]["metadata"]["market_structure"]["structure_bias"] == "bullish_breakout"


def test_signal_runtime_applies_htf_conflict_penalty_to_decision_confidence() -> None:
    """HTF 方向冲突时，decision 的 confidence 被惩罚，持久化记录反映最终值。"""
    source = DummySnapshotSource()
    service = DummySignalService()
    # sma_trend: sma > ema → buy, confidence=0.8
    htf_direction_calls = []

    def mock_htf_direction(symbol, timeframe):
        htf_direction_calls.append((symbol, timeframe))
        return "sell"  # 冲突：策略 buy，HTF sell

    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="sma_trend")],
        enable_confirmed_snapshot=True,
        enable_intrabar=False,
        htf_direction_fn=mock_htf_direction,
        htf_conflict_penalty=0.70,
        htf_alignment_boost=1.10,
    )

    runtime._on_snapshot(
        "XAUUSD",
        "M5",
        datetime.now(timezone.utc),
        {"sma20": {"sma": 201.0}, "ema50": {"ema": 200.0}, "atr14": {"atr": 5.0}},
        "confirmed",
    )
    runtime.process_next_event(timeout=0.01)

    assert htf_direction_calls == [("XAUUSD", "M5")]
    # 持久化的 decision 应已包含 HTF 惩罚后的 confidence
    persisted = service.persist_calls[0]
    metadata = persisted["metadata"]
    assert metadata.get("htf_direction") == "sell"
    assert metadata.get("htf_alignment") == "conflict"


def test_signal_runtime_applies_htf_alignment_boost_to_decision_confidence() -> None:
    """HTF 方向一致时，decision 的 confidence 被加成。"""
    source = DummySnapshotSource()
    service = DummySignalService()

    def mock_htf_direction(symbol, timeframe):
        return "buy"  # 对齐：策略 buy，HTF buy

    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="sma_trend")],
        enable_confirmed_snapshot=True,
        enable_intrabar=False,
        htf_direction_fn=mock_htf_direction,
        htf_conflict_penalty=0.70,
        htf_alignment_boost=1.10,
    )

    runtime._on_snapshot(
        "XAUUSD",
        "M5",
        datetime.now(timezone.utc),
        {"sma20": {"sma": 201.0}, "ema50": {"ema": 200.0}, "atr14": {"atr": 5.0}},
        "confirmed",
    )
    runtime.process_next_event(timeout=0.01)

    persisted = service.persist_calls[0]
    metadata = persisted["metadata"]
    assert metadata.get("htf_alignment") == "aligned"
    assert metadata.get("htf_confidence_multiplier") == 1.10


def test_signal_runtime_skips_htf_when_direction_fn_is_none() -> None:
    """未配置 HTF direction 函数时不修改 confidence。"""
    source = DummySnapshotSource()
    service = DummySignalService()

    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="sma_trend")],
        enable_confirmed_snapshot=True,
        enable_intrabar=False,
        htf_direction_fn=None,
    )

    runtime._on_snapshot(
        "XAUUSD",
        "M5",
        datetime.now(timezone.utc),
        {"sma20": {"sma": 201.0}, "ema50": {"ema": 200.0}, "atr14": {"atr": 5.0}},
        "confirmed",
    )
    runtime.process_next_event(timeout=0.01)

    persisted = service.persist_calls[0]
    # 没有 HTF 修正 metadata
    assert "htf_direction" not in persisted.get("metadata", {})


# ---------------------------------------------------------------------------
# _compute_htf_alignment multi-dimensional weight tests
# ---------------------------------------------------------------------------

def _make_runtime_with_htf_context(context_fn):
    """Create a minimal runtime with htf_context_fn for alignment tests."""
    source = DummySnapshotSource()
    service = DummySignalService()
    return SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="sma_trend")],
        enable_confirmed_snapshot=True,
        htf_context_fn=context_fn,
        htf_conflict_penalty=0.70,
        htf_alignment_boost=1.10,
    )


def _make_htf_context(direction="buy", confidence=0.5, regime="uncertain", stable_bars=1):
    from src.signals.strategies.htf_cache import HTFDirectionContext
    from datetime import datetime, timezone
    return HTFDirectionContext(
        direction=direction,
        confidence=confidence,
        regime=regime,
        stable_bars=stable_bars,
        updated_at=datetime.now(timezone.utc),
    )


def test_htf_alignment_no_context_returns_none():
    """No htf_context_fn → (None, None)."""
    rt = SignalRuntime(
        service=DummySignalService(),
        snapshot_source=DummySnapshotSource(),
        targets=[SignalTarget("XAUUSD", "M5", "sma_trend")],
    )
    mul, direction = rt._compute_htf_alignment("XAUUSD", "M5", "buy", "confirmed")
    assert mul is None
    assert direction is None


def test_htf_alignment_aligned_confirmed_base():
    """Aligned + default confidence(0.5) + stable_bars=1 → base boost (1.10)."""
    ctx = _make_htf_context(direction="buy", confidence=0.5, stable_bars=1)
    rt = _make_runtime_with_htf_context(lambda s, tf: ctx)
    mul, direction = rt._compute_htf_alignment("XAUUSD", "M5", "buy", "confirmed")
    assert direction == "buy"
    # strength=1.0, stability=1.0 → mul = 1.10 * 1.0 * 1.0 = 1.10
    assert abs(mul - 1.10) < 0.01


def test_htf_alignment_conflict_confirmed_base():
    """Conflict + default confidence(0.5) + stable_bars=1 → base penalty (0.70)."""
    ctx = _make_htf_context(direction="sell", confidence=0.5, stable_bars=1)
    rt = _make_runtime_with_htf_context(lambda s, tf: ctx)
    mul, direction = rt._compute_htf_alignment("XAUUSD", "M5", "buy", "confirmed")
    assert direction == "sell"
    assert abs(mul - 0.70) < 0.01


def test_htf_alignment_high_confidence_amplifies():
    """High HTF confidence (0.8) amplifies alignment effect."""
    ctx = _make_htf_context(direction="buy", confidence=0.8, stable_bars=1)
    rt = _make_runtime_with_htf_context(lambda s, tf: ctx)
    mul, _ = rt._compute_htf_alignment("XAUUSD", "M5", "buy", "confirmed")
    # strength = 1.0 + (0.8 - 0.5) * 0.3 = 1.09
    # mul = 1.10 * 1.09 = 1.199
    assert mul > 1.10, f"High conf should amplify boost beyond 1.10, got {mul}"


def test_htf_alignment_low_confidence_dampens():
    """Low HTF confidence (0.2) dampens alignment effect."""
    ctx = _make_htf_context(direction="buy", confidence=0.2, stable_bars=1)
    rt = _make_runtime_with_htf_context(lambda s, tf: ctx)
    mul, _ = rt._compute_htf_alignment("XAUUSD", "M5", "buy", "confirmed")
    # strength = 1.0 + (0.2 - 0.5) * 0.3 = 0.91
    # mul = 1.10 * 0.91 = 1.001
    assert mul < 1.10, f"Low conf should dampen boost, got {mul}"


def test_htf_alignment_high_stable_bars_amplifies():
    """Many stable bars amplify the effect."""
    ctx = _make_htf_context(direction="buy", confidence=0.5, stable_bars=6)
    rt = _make_runtime_with_htf_context(lambda s, tf: ctx)
    mul, _ = rt._compute_htf_alignment("XAUUSD", "M5", "buy", "confirmed")
    # stability = min(1.0 + (6-1)*0.03, 1.15) = min(1.15, 1.15) = 1.15
    # mul = 1.10 * 1.0 * 1.15 = 1.265
    assert mul > 1.20, f"High stable_bars should amplify, got {mul}"


def test_htf_alignment_stable_bars_capped():
    """stable_bars effect capped at 1.15."""
    ctx5 = _make_htf_context(direction="buy", confidence=0.5, stable_bars=6)
    ctx50 = _make_htf_context(direction="buy", confidence=0.5, stable_bars=50)
    rt5 = _make_runtime_with_htf_context(lambda s, tf: ctx5)
    rt50 = _make_runtime_with_htf_context(lambda s, tf: ctx50)
    mul5, _ = rt5._compute_htf_alignment("XAUUSD", "M5", "buy", "confirmed")
    mul50, _ = rt50._compute_htf_alignment("XAUUSD", "M5", "buy", "confirmed")
    assert abs(mul5 - mul50) < 0.01, "Stability should be capped"


def test_htf_alignment_intrabar_half_strength():
    """Intrabar scope halves the deviation from 1.0."""
    ctx = _make_htf_context(direction="buy", confidence=0.5, stable_bars=1)
    rt = _make_runtime_with_htf_context(lambda s, tf: ctx)
    confirmed_mul, _ = rt._compute_htf_alignment("XAUUSD", "M5", "buy", "confirmed")
    intrabar_mul, _ = rt._compute_htf_alignment("XAUUSD", "M5", "buy", "intrabar")
    # confirmed: 1.10, intrabar: 1.0 + (1.10 - 1.0) * 0.5 = 1.05
    assert abs(intrabar_mul - 1.05) < 0.01
    assert abs(confirmed_mul - intrabar_mul) > 0.03, "Intrabar should be less than confirmed"


def test_htf_alignment_intrabar_conflict_half_strength():
    """Intrabar conflict penalty is also halved."""
    ctx = _make_htf_context(direction="sell", confidence=0.5, stable_bars=1)
    rt = _make_runtime_with_htf_context(lambda s, tf: ctx)
    confirmed_mul, _ = rt._compute_htf_alignment("XAUUSD", "M5", "buy", "confirmed")
    intrabar_mul, _ = rt._compute_htf_alignment("XAUUSD", "M5", "buy", "intrabar")
    # confirmed: 0.70, intrabar: 1.0 + (0.70 - 1.0) * 0.5 = 0.85
    assert abs(intrabar_mul - 0.85) < 0.01
    assert intrabar_mul > confirmed_mul, "Intrabar conflict should be less severe"


def test_htf_alignment_fallback_to_direction_fn():
    """When context_fn returns None, falls back to direction_fn."""
    rt = SignalRuntime(
        service=DummySignalService(),
        snapshot_source=DummySnapshotSource(),
        targets=[SignalTarget("XAUUSD", "M5", "sma_trend")],
        htf_context_fn=lambda s, tf: None,
        htf_direction_fn=lambda s, tf: "buy",
        htf_alignment_boost=1.10,
        htf_conflict_penalty=0.70,
    )
    mul, direction = rt._compute_htf_alignment("XAUUSD", "M5", "buy", "confirmed")
    assert direction == "buy"
    assert abs(mul - 1.10) < 0.01


def test_htf_alignment_combined_factors():
    """Combined: high confidence + high stable bars + aligned → strong boost."""
    ctx = _make_htf_context(direction="sell", confidence=0.9, stable_bars=5)
    rt = _make_runtime_with_htf_context(lambda s, tf: ctx)
    mul, _ = rt._compute_htf_alignment("XAUUSD", "M5", "sell", "confirmed")
    # strength = 1.0 + (0.9 - 0.5) * 0.3 = 1.12
    # stability = min(1.0 + 4*0.03, 1.15) = 1.12
    # mul = 1.10 * 1.12 * 1.12 = 1.38
    assert mul > 1.30, f"Combined strong factors should give significant boost, got {mul}"
