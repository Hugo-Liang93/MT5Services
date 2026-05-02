from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.backtesting.engine.signals import process_decision
from src.backtesting.state_edge_overlay import StateEdgeBacktestOverlay
from src.clients.mt5_market import OHLC
from src.research.state_edge.artifacts import StateEdgeArtifact, StateEdgePrediction
from src.signals.models import SignalDecision


def _artifact() -> StateEdgeArtifact:
    return StateEdgeArtifact(
        model_id="state-edge-test",
        symbol="XAUUSD",
        timeframe="H1",
        backend="cpu",
        feature_keys=[],
        label_summary={"long": 1, "short": 1, "no_trade": 0},
        metrics={},
        predictions=[
            StateEdgePrediction(
                bar_time="2026-01-01T00:00:00+00:00",
                long_edge_prob=0.72,
                short_edge_prob=0.10,
                no_trade_prob=0.18,
            ),
            StateEdgePrediction(
                bar_time="2026-01-01T01:00:00+00:00",
                long_edge_prob=0.20,
                short_edge_prob=0.68,
                no_trade_prob=0.12,
            ),
        ],
    )


def test_shadow_mode_records_without_blocking() -> None:
    overlay = StateEdgeBacktestOverlay(_artifact(), mode="shadow", threshold=0.65)

    verdict = overlay.evaluate(
        datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        "buy",
        strategy="s",
        confidence=0.8,
    )

    assert verdict.allowed is True
    assert verdict.direction_probability == 0.20
    report = overlay.report()
    assert report["observed"] == 1
    assert report["blocked"] == 0
    assert report["missing_predictions"] == 0


def test_filter_mode_blocks_low_direction_probability() -> None:
    overlay = StateEdgeBacktestOverlay(_artifact(), mode="filter", threshold=0.65)

    blocked = overlay.evaluate(
        datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        "buy",
        strategy="s",
        confidence=0.8,
    )
    allowed = overlay.evaluate(
        datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        "sell",
        strategy="s",
        confidence=0.8,
    )

    assert blocked.allowed is False
    assert blocked.reason == "state_edge_probability_below_threshold"
    assert allowed.allowed is True
    assert overlay.report()["blocked_by_direction"] == {"buy": 1}


def test_filter_mode_can_target_only_selected_directions() -> None:
    overlay = StateEdgeBacktestOverlay(
        _artifact(),
        mode="filter",
        threshold=0.65,
        filter_directions=("buy",),
    )

    blocked_buy = overlay.evaluate(
        datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        "buy",
        strategy="s",
        confidence=0.8,
    )
    allowed_sell = overlay.evaluate(
        datetime(2026, 1, 1, 0, tzinfo=timezone.utc),
        "sell",
        strategy="s",
        confidence=0.8,
    )

    assert blocked_buy.allowed is False
    assert blocked_buy.reason == "state_edge_probability_below_threshold"
    assert allowed_sell.allowed is True
    assert allowed_sell.reason == "state_edge_direction_not_filtered"
    report = overlay.report()
    assert report["filter_directions"] == ["buy"]
    assert report["blocked_by_direction"] == {"buy": 1}


def test_process_decision_uses_formal_overlay_port_before_entry() -> None:
    overlay = StateEdgeBacktestOverlay(_artifact(), mode="filter", threshold=0.65)
    rejections: list[str] = []
    blocked_events: list[dict] = []
    engine = _ProcessDecisionEngine(
        state_edge_overlay=overlay,
        rejections=rejections,
        blocked_events=blocked_events,
    )
    decision = SignalDecision(
        strategy="s",
        symbol="XAUUSD",
        timeframe="H1",
        direction="buy",
        confidence=0.9,
        reason="test",
    )
    bar = OHLC(
        symbol="XAUUSD",
        timeframe="H1",
        time=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        open=1.0,
        high=1.0,
        low=1.0,
        close=1.0,
        volume=1.0,
    )

    process_decision(engine, decision, bar, 1, {"atr14": {"atr": 1.0}}, "unknown")

    assert rejections == ["state_edge_filter"]
    assert blocked_events == [
        {
            "source": "state_edge_overlay",
            "execution_reason": "state_edge_filter",
            "reason": "state_edge_probability_below_threshold",
            "bar_time": "2026-01-01T01:00:00+00:00",
            "bar_index": 1,
            "strategy": "s",
            "direction": "buy",
            "confidence": 0.9,
            "regime": "unknown",
            "price": 1.0,
            "direction_probability": 0.20,
            "threshold": 0.65,
            "state_edge_probabilities": {
                "long": 0.20,
                "short": 0.68,
                "no_trade": 0.12,
            },
        }
    ]
    assert overlay.report()["blocked"] == 1


def test_process_decision_does_not_probe_private_state_edge_field() -> None:
    engine = _NoPrivateStateEdgeProbeEngine()
    decision = SignalDecision(
        strategy="s",
        symbol="XAUUSD",
        timeframe="H1",
        direction="buy",
        confidence=0.9,
        reason="test",
    )
    bar = OHLC(
        symbol="XAUUSD",
        timeframe="H1",
        time=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        open=1.0,
        high=1.0,
        low=1.0,
        close=1.0,
        volume=1.0,
    )

    process_decision(engine, decision, bar, 1, {"atr14": {"atr": 0.0}}, "unknown")

    assert engine.rejections == []
    assert engine.blocked_events == []


class _ProcessDecisionEngine:
    def __init__(
        self,
        *,
        state_edge_overlay: object | None,
        rejections: list[str],
        blocked_events: list[dict],
    ) -> None:
        self._config = SimpleNamespace(
            enable_state_machine=False,
            confidence=SimpleNamespace(min_confidence=0.1),
        )
        self._circuit_breaker = None
        self._portfolio = SimpleNamespace(_open_positions=[])
        self._strategy_deployments = {}
        self._pending_entry_enabled = False
        self._state_edge_overlay_value = state_edge_overlay
        self._rejections = rejections
        self._blocked_events = blocked_events

    @property
    def state_edge_overlay(self) -> object | None:
        return self._state_edge_overlay_value

    @property
    def entry_meta_overlay(self) -> None:
        return None

    def record_execution_rejection(self, reason: str) -> None:
        self._rejections.append(reason)

    def record_blocked_entry(self, event: dict) -> None:
        self._blocked_events.append(event)


class _NoPrivateStateEdgeProbeEngine:
    def __init__(self) -> None:
        self._config = SimpleNamespace(
            enable_state_machine=False,
            confidence=SimpleNamespace(min_confidence=0.1),
        )
        self._circuit_breaker = None
        self._portfolio = SimpleNamespace(_open_positions=[])
        self._strategy_deployments = {}
        self._pending_entry_enabled = False
        self.rejections: list[str] = []
        self.blocked_events: list[dict] = []

    def __getattribute__(self, name: str):
        if name == "_state_edge_overlay":
            raise AssertionError("process_decision must not probe _state_edge_overlay")
        return object.__getattribute__(self, name)

    @property
    def state_edge_overlay(self) -> None:
        return None

    @property
    def entry_meta_overlay(self) -> None:
        return None

    def record_execution_rejection(self, reason: str) -> None:
        self.rejections.append(reason)

    def record_blocked_entry(self, event: dict) -> None:
        self.blocked_events.append(event)
