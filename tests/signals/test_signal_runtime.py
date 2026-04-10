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
            direction=action,
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

    def get_strategy(self, name: str):
        return None  # Dummy: no real strategy objects

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


def test_signal_runtime_assigns_transient_signal_id_for_repeated_confirmed_actionable_events() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="sma_trend")],
        enable_confirmed_snapshot=True,

    )
    captured_events = []
    runtime.add_signal_listener(captured_events.append)

    indicators = {
        "sma20": {"sma": 201.0},
        "ema50": {"ema": 200.0},
        "atr14": {"atr": 5.0},
    }
    first_bar = datetime(2026, 3, 19, 10, 0, tzinfo=timezone.utc)
    second_bar = datetime(2026, 3, 19, 10, 5, tzinfo=timezone.utc)

    runtime._on_snapshot("XAUUSD", "M5", first_bar, indicators, "confirmed")
    runtime.process_next_event(timeout=0.01)
    runtime._on_snapshot("XAUUSD", "M5", second_bar, indicators, "confirmed")
    runtime.process_next_event(timeout=0.01)

    assert len(captured_events) == 2
    assert captured_events[0].signal_id
    assert captured_events[1].signal_id
    assert captured_events[0].signal_id != captured_events[1].signal_id
    assert captured_events[0].metadata["state_changed"] is True
    assert captured_events[1].metadata["state_changed"] is False


def test_signal_runtime_status_exposes_trigger_mode() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="sma_trend")],
        enable_confirmed_snapshot=True,

    )

    status = runtime.status()

    assert status["target_count"] == 1
    assert status["trigger_mode"]["confirmed_snapshot"] is True
    assert "intrabar" not in status["trigger_mode"]
    assert "confirmed_backpressure_waits" in status
    assert "confirmed_backpressure_failures" in status


def test_signal_runtime_processes_intrabar_snapshot_when_enabled() -> None:
    """Intrabar snapshots are evaluated but no longer produce state machine
    transitions (preview/armed removed). Only the coordinator path is active."""
    source = DummySnapshotSource()
    service = DummySignalService()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[
            SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion")
        ],
        enable_confirmed_snapshot=True,
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
    # No state machine transition for intrabar — persist_calls empty (no coordinator)
    assert len(service.persist_calls) == 0


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
            "direction": "buy",
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
    assert len(service.persist_calls) == 0


def test_signal_runtime_fuses_intrabar_and_confirmed_votes_per_strategy() -> None:
    source = DummySnapshotSource()
    service = DummySignalService()
    runtime = SignalRuntime(
        service=service,
        snapshot_source=source,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="rsi_reversion")],
        enable_confirmed_snapshot=True,

    )

    intrabar_decision = SignalDecision(
        strategy="rsi_reversion",
        symbol="XAUUSD",
        timeframe="M5",
        direction="buy",
        confidence=0.6,
        reason="intrabar",
    )
    confirmed_decision = SignalDecision(
        strategy="rsi_reversion",
        symbol="XAUUSD",
        timeframe="M5",
        direction="buy",
        confidence=0.9,
        reason="confirmed",
    )
    bar_time = datetime.now(timezone.utc)

    from src.signals.orchestration.vote_processor import fuse_vote_decisions

    first = fuse_vote_decisions(
        "XAUUSD", "M5", bar_time, "intrabar", [intrabar_decision],
        runtime._vote_fusion_cache,
    )
    second = fuse_vote_decisions(
        "XAUUSD", "M5", bar_time, "confirmed", [confirmed_decision],
        runtime._vote_fusion_cache,
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


    # 市场结构数据应注入到策略的 metadata 中
    assert service.evaluate_calls[0]["metadata"]["market_structure"]["structure_bias"] == "bullish_breakout"
