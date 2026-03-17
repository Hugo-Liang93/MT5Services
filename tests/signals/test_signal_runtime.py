from __future__ import annotations

from datetime import datetime, timezone

from src.signals.runtime import SignalRuntime, SignalTarget


class DummyMarketService:
    def __init__(self):
        self.close_listeners = []
        self.intrabar_listeners = []

    def add_ohlc_close_listener(self, listener):
        self.close_listeners.append(listener)

    def remove_ohlc_close_listener(self, listener):
        self.close_listeners = [item for item in self.close_listeners if item is not listener]

    def add_intrabar_listener(self, listener):
        self.intrabar_listeners.append(listener)

    def remove_intrabar_listener(self, listener):
        self.intrabar_listeners = [item for item in self.intrabar_listeners if item is not listener]


class DummySignalService:
    def __init__(self):
        self.calls = []

    def evaluate(self, **kwargs):
        self.calls.append(kwargs)


def test_signal_runtime_processes_close_bar_event() -> None:
    market = DummyMarketService()
    service = DummySignalService()
    runtime = SignalRuntime(
        service=service,
        market_service=market,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="sma_trend")],
        enable_close_bar=True,
        enable_intrabar=False,
    )

    runtime._on_close_bar("XAUUSD", "M5", datetime.now(timezone.utc))
    processed = runtime.process_next_event(timeout=0.01)

    assert processed is True
    assert len(service.calls) == 1
    assert service.calls[0]["strategy"] == "sma_trend"
    assert service.calls[0]["persist"] is True


def test_signal_runtime_status_exposes_trigger_mode() -> None:
    market = DummyMarketService()
    service = DummySignalService()
    runtime = SignalRuntime(
        service=service,
        market_service=market,
        targets=[SignalTarget(symbol="XAUUSD", timeframe="M5", strategy="sma_trend")],
        enable_close_bar=True,
        enable_intrabar=True,
    )

    status = runtime.status()

    assert status["target_count"] == 1
    assert status["trigger_mode"]["close_bar"] is True
    assert status["trigger_mode"]["intrabar"] is True
