from __future__ import annotations

from datetime import datetime, timezone

from src.config.mt5 import MT5Settings
from src.config.runtime_identity import RuntimeIdentity, build_account_key
from src.monitoring.pipeline import (
    PIPELINE_INTENT_CLAIMED,
    PipelineEventBus,
)
from src.signals.contracts import StrategyDeployment, StrategyDeploymentStatus
from src.signals.models import SignalEvent
from src.trading.intents.consumer import ExecutionIntentConsumer
from src.trading.intents.publisher import ExecutionIntentPublisher


def _runtime_identity(
    *,
    instance_role: str = "main",
    live_topology_mode: str = "single_account",
    instance_name: str = "live-main",
    environment: str = "live",
    account_alias: str = "main",
    login: int = 1001,
) -> RuntimeIdentity:
    return RuntimeIdentity(
        instance_name=instance_name,
        environment=environment,
        instance_id=f"{instance_role}-{instance_name}",
        instance_role=instance_role,
        live_topology_mode=live_topology_mode,
        account_alias=account_alias,
        account_label=account_alias.title(),
        account_key=build_account_key(environment, "Broker-Live", login),
        mt5_server="Broker-Live",
        mt5_login=login,
        mt5_path=f"C:/MT5/{account_alias}/terminal64.exe",
    )


def _event(
    strategy: str = "trend_alpha",
    *,
    scope: str = "confirmed",
    signal_state: str = "confirmed_buy",
    signal_id: str = "sig-1",
) -> SignalEvent:
    return SignalEvent(
        symbol="XAUUSD",
        timeframe="M5",
        strategy=strategy,
        direction="buy",
        confidence=0.73,
        signal_state=signal_state,
        scope=scope,
        indicators={"atr14": {"atr": 3.2}},
        metadata={"signal_trace_id": "trace-1"},
        generated_at=datetime(2026, 4, 12, 8, 0, tzinfo=timezone.utc),
        signal_id=signal_id,
        reason="test",
    )


def test_execution_intent_publisher_requires_explicit_binding_in_single_account_mode(monkeypatch):
    rows: list[tuple] = []
    monkeypatch.setattr(
        "src.trading.intents.publisher.load_group_mt5_settings",
        lambda **kwargs: {
            "main": MT5Settings(
                instance_name="live-main",
                account_alias="main",
                account_label="Main",
                mt5_login=1001,
                mt5_server="Broker-Live",
                mt5_path="C:/MT5/main/terminal64.exe",
            )
        },
    )
    publisher = ExecutionIntentPublisher(
        write_fn=lambda batch: rows.extend(batch),
        runtime_identity=_runtime_identity(),
        account_bindings={},
        strategy_deployments={
            "trend_alpha": StrategyDeployment(
                name="trend_alpha",
                status=StrategyDeploymentStatus.ACTIVE_GUARDED,
            )
        },
        auto_trade_enabled=True,
    )

    publisher.on_signal_event(_event())

    assert rows == []


def test_execution_intent_publisher_routes_explicit_single_account_binding(monkeypatch):
    rows: list[tuple] = []
    monkeypatch.setattr(
        "src.trading.intents.publisher.load_group_mt5_settings",
        lambda **kwargs: {
            "main": MT5Settings(
                instance_name="live-main",
                account_alias="main",
                account_label="Main",
                mt5_login=1001,
                mt5_server="Broker-Live",
                mt5_path="C:/MT5/main/terminal64.exe",
            )
        },
    )
    publisher = ExecutionIntentPublisher(
        write_fn=lambda batch: rows.extend(batch),
        runtime_identity=_runtime_identity(),
        account_bindings={"main": ["trend_alpha"]},
        strategy_deployments={
            "trend_alpha": StrategyDeployment(
                name="trend_alpha",
                status=StrategyDeploymentStatus.ACTIVE_GUARDED,
            )
        },
        auto_trade_enabled=True,
    )

    publisher.on_signal_event(_event())

    assert len(rows) == 1
    assert rows[0][2] == f"sig-1:{build_account_key('live', 'Broker-Live', 1001)}"
    assert rows[0][3] == "sig-1"
    assert rows[0][4] == build_account_key("live", "Broker-Live", 1001)
    assert rows[0][5] == "main"
    assert rows[0][9]["strategy"] == "trend_alpha"


def test_execution_intent_publisher_routes_bound_strategy_in_multi_account_mode(monkeypatch):
    rows: list[tuple] = []
    monkeypatch.setattr(
        "src.trading.intents.publisher.load_group_mt5_settings",
        lambda **kwargs: {
            "main": MT5Settings(
                instance_name="live-main",
                account_alias="main",
                account_label="Main",
                mt5_login=1001,
                mt5_server="Broker-Live",
                mt5_path="C:/MT5/main/terminal64.exe",
            ),
            "exec_b": MT5Settings(
                instance_name="live-exec-b",
                account_alias="exec_b",
                account_label="Exec B",
                mt5_login=1002,
                mt5_server="Broker-Live",
                mt5_path="C:/MT5/exec_b/terminal64.exe",
            ),
        },
    )
    publisher = ExecutionIntentPublisher(
        write_fn=lambda batch: rows.extend(batch),
        runtime_identity=_runtime_identity(live_topology_mode="multi_account"),
        account_bindings={"exec_b": ["trend_alpha"]},
        strategy_deployments={
            "trend_alpha": StrategyDeployment(
                name="trend_alpha",
                status=StrategyDeploymentStatus.ACTIVE_GUARDED,
            )
        },
        auto_trade_enabled=True,
    )

    publisher.on_signal_event(_event())

    assert len(rows) == 1
    assert rows[0][5] == "exec_b"
    assert rows[0][4] == build_account_key("live", "Broker-Live", 1002)


def test_execution_intent_publisher_routes_intrabar_armed_signal(monkeypatch):
    rows: list[tuple] = []
    monkeypatch.setattr(
        "src.trading.intents.publisher.load_group_mt5_settings",
        lambda **kwargs: {
            "exec_b": MT5Settings(
                instance_name="live-exec-b",
                account_alias="exec_b",
                account_label="Exec B",
                mt5_login=1002,
                mt5_server="Broker-Live",
                mt5_path="C:/MT5/exec_b/terminal64.exe",
            ),
        },
    )
    publisher = ExecutionIntentPublisher(
        write_fn=lambda batch: rows.extend(batch),
        runtime_identity=_runtime_identity(live_topology_mode="multi_account"),
        account_bindings={"exec_b": ["trend_alpha"]},
        strategy_deployments={
            "trend_alpha": StrategyDeployment(
                name="trend_alpha",
                status=StrategyDeploymentStatus.ACTIVE_GUARDED,
            )
        },
        auto_trade_enabled=True,
    )

    publisher.on_signal_event(
        _event(
            scope="intrabar",
            signal_state="intrabar_armed_buy",
            signal_id="sig-intrabar-1",
        )
    )

    assert len(rows) == 1
    assert rows[0][3] == "sig-intrabar-1"
    assert rows[0][8] == "M5"
    assert rows[0][9]["scope"] == "intrabar"
    assert rows[0][9]["signal_state"] == "intrabar_armed_buy"


def test_execution_intent_publisher_skips_non_actionable_intrabar_signal(monkeypatch):
    rows: list[tuple] = []
    monkeypatch.setattr(
        "src.trading.intents.publisher.load_group_mt5_settings",
        lambda **kwargs: {
            "exec_b": MT5Settings(
                instance_name="live-exec-b",
                account_alias="exec_b",
                account_label="Exec B",
                mt5_login=1002,
                mt5_server="Broker-Live",
                mt5_path="C:/MT5/exec_b/terminal64.exe",
            ),
        },
    )
    publisher = ExecutionIntentPublisher(
        write_fn=lambda batch: rows.extend(batch),
        runtime_identity=_runtime_identity(live_topology_mode="multi_account"),
        account_bindings={"exec_b": ["trend_alpha"]},
        strategy_deployments={
            "trend_alpha": StrategyDeployment(
                name="trend_alpha",
                status=StrategyDeploymentStatus.ACTIVE_GUARDED,
            )
        },
        auto_trade_enabled=True,
    )

    publisher.on_signal_event(
        _event(
            scope="intrabar",
            signal_state="preview_buy",
            signal_id="sig-preview-1",
        )
    )

    assert rows == []


def test_execution_intent_publisher_skips_non_live_deployments(monkeypatch):
    rows: list[tuple] = []
    monkeypatch.setattr(
        "src.trading.intents.publisher.load_group_mt5_settings",
        lambda **kwargs: {
            "main": MT5Settings(
                instance_name="live-main",
                account_alias="main",
                account_label="Main",
                mt5_login=1001,
                mt5_server="Broker-Live",
                mt5_path="C:/MT5/main/terminal64.exe",
            )
        },
    )
    publisher = ExecutionIntentPublisher(
        write_fn=lambda batch: rows.extend(batch),
        runtime_identity=_runtime_identity(),
        account_bindings={"main": ["trend_alpha"]},
        strategy_deployments={
            "trend_alpha": StrategyDeployment(
                name="trend_alpha",
                status=StrategyDeploymentStatus.PAPER_ONLY,
            )
        },
        auto_trade_enabled=True,
    )

    publisher.on_signal_event(_event())

    assert rows == []


def test_execution_intent_consumer_processes_and_completes_claimed_intent():
    completed: list[dict] = []
    processed: list[SignalEvent] = []
    received = []
    pipeline_bus = PipelineEventBus()
    pipeline_bus.add_listener(received.append)

    class _Executor:
        def process_event(self, event: SignalEvent):
            processed.append(event)
            return {"status": "ok", "signal_id": event.signal_id}

    consumer = ExecutionIntentConsumer(
        claim_fn=lambda **kwargs: [],
        complete_fn=lambda **kwargs: completed.append(kwargs),
        runtime_identity=_runtime_identity(instance_role="executor", live_topology_mode="multi_account"),
        trade_executor=_Executor(),
        pipeline_event_bus=pipeline_bus,
    )

    consumer._process_intent(
        {
            "intent_id": "intent-1",
            "signal_id": "sig-1",
            "strategy": "trend_alpha",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "target_account_key": build_account_key("live", "Broker-Live", 1001),
            "target_account_alias": "main",
            "payload": {
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "strategy": "trend_alpha",
                "direction": "buy",
                "confidence": 0.73,
                "signal_state": "confirmed_buy",
                "scope": "confirmed",
                "indicators": {"atr14": {"atr": 3.2}},
                "metadata": {},
                "generated_at": "2026-04-12T08:00:00+00:00",
                "signal_id": "sig-1",
                "reason": "test",
                "parent_bar_time": None,
            },
        }
    )

    assert len(processed) == 1
    assert processed[0].metadata["intent_id"] == "intent-1"
    assert processed[0].metadata["target_account_alias"] == "main"
    assert processed[0].metadata["signal_trace_id"] == "sig-1"
    assert completed == [
        {
            "intent_id": "intent-1",
            "status": "completed",
            "decision_metadata": {
                "claimed_by_instance_id": "executor-live-main",
                "claimed_by_run_id": "executor-live-main",
                "result": {"status": "ok", "signal_id": "sig-1"},
            },
            "last_error_code": None,
        }
    ]
    assert [event.type for event in received] == ["execution_succeeded"]
    assert received[0].trace_id == "sig-1"
    assert received[0].payload["account_key"] == build_account_key("live", "Broker-Live", 1001)
    assert received[0].payload["instance_id"] == "executor-live-main"
    assert received[0].payload["status"] == "completed"


def test_execution_intent_consumer_marks_intrabar_intent_skipped_when_executor_returns_none():
    completed: list[dict] = []
    received = []
    pipeline_bus = PipelineEventBus()
    pipeline_bus.add_listener(received.append)

    class _Executor:
        def process_event(self, event: SignalEvent):
            assert event.scope == "intrabar"
            return None

    consumer = ExecutionIntentConsumer(
        claim_fn=lambda **kwargs: [],
        complete_fn=lambda **kwargs: completed.append(kwargs),
        runtime_identity=_runtime_identity(
            instance_role="executor",
            live_topology_mode="multi_account",
        ),
        trade_executor=_Executor(),
        pipeline_event_bus=pipeline_bus,
    )

    consumer._process_intent(
        {
            "intent_id": "intent-intrabar-1",
            "signal_id": "sig-intrabar-1",
            "strategy": "trend_alpha",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "target_account_key": build_account_key("live", "Broker-Live", 1001),
            "target_account_alias": "main",
            "payload": {
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "strategy": "trend_alpha",
                "direction": "buy",
                "confidence": 0.73,
                "signal_state": "intrabar_armed_buy",
                "scope": "intrabar",
                "indicators": {"atr14": {"atr": 3.2}},
                "metadata": {"signal_trace_id": "trace-intrabar-1"},
                "generated_at": "2026-04-12T08:00:00+00:00",
                "signal_id": "sig-intrabar-1",
                "reason": "test",
                "parent_bar_time": "2026-04-12T08:00:00+00:00",
            },
        }
    )

    assert completed == [
        {
            "intent_id": "intent-intrabar-1",
            "status": "skipped",
            "decision_metadata": {
                "claimed_by_instance_id": "executor-live-main",
                "claimed_by_run_id": "executor-live-main",
                "result": None,
            },
            "last_error_code": None,
        }
    ]
    assert [event.type for event in received] == ["execution_skipped"]
    assert received[0].trace_id == "trace-intrabar-1"
    assert received[0].payload["status"] == "skipped"
    assert received[0].payload["signal_scope"] == "intrabar"


def test_execution_intent_consumer_propagates_skip_reason_from_executor_result():
    completed: list[dict] = []
    received = []
    pipeline_bus = PipelineEventBus()
    pipeline_bus.add_listener(received.append)

    class _Executor:
        def process_event(self, event: SignalEvent):
            assert event.scope == "intrabar"
            return {
                "status": "skipped",
                "reason": "trade_params_unavailable",
                "skip_reason": "trade_params_unavailable",
                "category": "trade_params",
                "skip_category": "trade_params",
            }

    consumer = ExecutionIntentConsumer(
        claim_fn=lambda **kwargs: [],
        complete_fn=lambda **kwargs: completed.append(kwargs),
        runtime_identity=_runtime_identity(
            instance_role="executor",
            live_topology_mode="multi_account",
        ),
        trade_executor=_Executor(),
        pipeline_event_bus=pipeline_bus,
    )

    consumer._process_intent(
        {
            "intent_id": "intent-intrabar-2",
            "signal_id": "sig-intrabar-2",
            "strategy": "trend_alpha",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "target_account_key": build_account_key("live", "Broker-Live", 1001),
            "target_account_alias": "main",
            "payload": {
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "strategy": "trend_alpha",
                "direction": "buy",
                "confidence": 0.73,
                "signal_state": "intrabar_armed_buy",
                "scope": "intrabar",
                "indicators": {"atr14": {"atr": 3.2}},
                "metadata": {"signal_trace_id": "trace-intrabar-2"},
                "generated_at": "2026-04-12T08:00:00+00:00",
                "signal_id": "sig-intrabar-2",
                "reason": "test",
                "parent_bar_time": "2026-04-12T08:00:00+00:00",
            },
        }
    )

    assert completed[0]["status"] == "skipped"
    assert completed[0]["decision_metadata"]["result"]["reason"] == "trade_params_unavailable"
    assert [event.type for event in received] == ["execution_skipped"]
    assert received[0].payload["reason"] == "trade_params_unavailable"
    assert received[0].payload["skip_reason"] == "trade_params_unavailable"
    assert received[0].payload["category"] == "trade_params"
    assert received[0].payload["skip_category"] == "trade_params"


def test_execution_intent_consumer_marks_intent_failed_from_executor_result():
    completed: list[dict] = []
    received = []
    pipeline_bus = PipelineEventBus()
    pipeline_bus.add_listener(received.append)

    class _Executor:
        def process_event(self, event: SignalEvent):
            assert event.scope == "confirmed"
            return {
                "status": "failed",
                "reason": "execution_dispatch_failed",
                "category": "dispatch",
                "error_code": "RuntimeError",
                "details": {"error": "broker timeout"},
            }

    consumer = ExecutionIntentConsumer(
        claim_fn=lambda **kwargs: [],
        complete_fn=lambda **kwargs: completed.append(kwargs),
        runtime_identity=_runtime_identity(
            instance_role="executor",
            live_topology_mode="multi_account",
        ),
        trade_executor=_Executor(),
        pipeline_event_bus=pipeline_bus,
    )

    consumer._process_intent(
        {
            "intent_id": "intent-confirmed-failed-1",
            "signal_id": "sig-confirmed-failed-1",
            "strategy": "trend_alpha",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "target_account_key": build_account_key("live", "Broker-Live", 1001),
            "target_account_alias": "main",
            "payload": {
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "strategy": "trend_alpha",
                "direction": "buy",
                "confidence": 0.73,
                "signal_state": "confirmed_buy",
                "scope": "confirmed",
                "indicators": {"atr14": {"atr": 3.2}},
                "metadata": {"signal_trace_id": "trace-confirmed-failed-1"},
                "generated_at": "2026-04-12T08:00:00+00:00",
                "signal_id": "sig-confirmed-failed-1",
                "reason": "test",
            },
        }
    )

    assert completed == [
        {
            "intent_id": "intent-confirmed-failed-1",
            "status": "failed",
            "decision_metadata": {
                "claimed_by_instance_id": "executor-live-main",
                "claimed_by_run_id": "executor-live-main",
                "result": {
                    "status": "failed",
                    "reason": "execution_dispatch_failed",
                    "category": "dispatch",
                    "error_code": "RuntimeError",
                    "details": {"error": "broker timeout"},
                },
            },
            "last_error_code": "RuntimeError",
        }
    ]
    assert [event.type for event in received] == ["execution_failed"]
    assert received[0].payload["reason"] == "execution_dispatch_failed"
    assert received[0].payload["category"] == "dispatch"
    assert received[0].payload["error_code"] == "RuntimeError"


def test_execution_intent_consumer_uses_fallback_event_when_payload_decode_fails():
    completed: list[dict] = []
    received = []
    pipeline_bus = PipelineEventBus()
    pipeline_bus.add_listener(received.append)

    class _Executor:
        def process_event(self, event: SignalEvent):
            raise AssertionError("should not reach executor")

    consumer = ExecutionIntentConsumer(
        claim_fn=lambda **kwargs: [],
        complete_fn=lambda **kwargs: completed.append(kwargs),
        runtime_identity=_runtime_identity(
            instance_role="executor",
            live_topology_mode="multi_account",
        ),
        trade_executor=_Executor(),
        pipeline_event_bus=pipeline_bus,
    )

    consumer._process_intent(
        {
            "intent_id": "intent-bad-payload-1",
            "signal_id": "sig-bad-payload-1",
            "strategy": "trend_alpha",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "target_account_key": build_account_key("live", "Broker-Live", 1001),
            "target_account_alias": "main",
            "payload": {
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "strategy": "trend_alpha",
                "direction": "buy",
                "confidence": "not-a-number",
                "signal_state": "confirmed_buy",
                "scope": "confirmed",
                "metadata": {"signal_trace_id": "trace-bad-payload-1"},
                "signal_id": "sig-bad-payload-1",
            },
        }
    )

    assert completed[0]["status"] == "failed"
    assert completed[0]["last_error_code"] == "ValueError"
    assert [event.type for event in received] == ["execution_failed"]
    assert received[0].trace_id == "trace-bad-payload-1"
    assert received[0].payload["signal_id"] == "sig-bad-payload-1"
    assert received[0].payload["error_code"] == "ValueError"


def test_execution_intent_publisher_emits_intent_published_with_trace_context(monkeypatch):
    received = []
    pipeline_bus = PipelineEventBus()
    pipeline_bus.add_listener(received.append)
    monkeypatch.setattr(
        "src.trading.intents.publisher.load_group_mt5_settings",
        lambda **kwargs: {
            "main": MT5Settings(
                instance_name="live-main",
                account_alias="main",
                account_label="Main",
                mt5_login=1001,
                mt5_server="Broker-Live",
                mt5_path="C:/MT5/main/terminal64.exe",
            )
        },
    )
    publisher = ExecutionIntentPublisher(
        write_fn=lambda rows: None,
        runtime_identity=_runtime_identity(),
        account_bindings={"main": ["trend_alpha"]},
        strategy_deployments={
            "trend_alpha": StrategyDeployment(
                name="trend_alpha",
                status=StrategyDeploymentStatus.ACTIVE_GUARDED,
            )
        },
        auto_trade_enabled=True,
        pipeline_event_bus=pipeline_bus,
    )

    publisher.on_signal_event(_event())

    assert len(received) == 1
    assert received[0].type == "intent_published"
    assert received[0].trace_id == "trace-1"
    assert received[0].payload["trace_id"] == "trace-1"
    assert received[0].payload["signal_scope"] == "confirmed"
    assert received[0].payload["signal_state"] == "confirmed_buy"
    assert received[0].payload["published_by_instance_id"] == "main-live-main"
    assert received[0].payload["instance_role"] == "main"


def test_execution_intent_consumer_emits_reclaim_and_dead_letter_events(monkeypatch):
    received = []
    pipeline_bus = PipelineEventBus()
    pipeline_bus.add_listener(received.append)
    completed = []
    template_payload = {
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "strategy": "trend_alpha",
        "direction": "buy",
        "confidence": 0.73,
        "signal_state": "confirmed_buy",
        "scope": "confirmed",
        "indicators": {"atr14": {"atr": 3.2}},
        "metadata": {"signal_trace_id": "trace-intent-1"},
        "generated_at": "2026-04-12T08:00:00+00:00",
        "signal_id": "sig-1",
        "reason": "test",
        "parent_bar_time": None,
    }

    class _Executor:
        def process_event(self, event: SignalEvent):
            raise AssertionError("should not process claimed intents in this test")

    consumer = ExecutionIntentConsumer(
        claim_fn=lambda **kwargs: [],
        complete_fn=lambda **kwargs: completed.append(kwargs),
        runtime_identity=_runtime_identity(instance_role="executor", live_topology_mode="multi_account"),
        trade_executor=_Executor(),
        pipeline_event_bus=pipeline_bus,
    )

    calls = {"count": 0}

    def _claim_fn(**kwargs):
        calls["count"] += 1
        consumer._stop_event.set()
        return {
            "claimed": [],
            "reclaimed": [
                {
                    "intent_id": "intent-reclaimed",
                    "intent_key": "sig-1:key",
                    "signal_id": "sig-1",
                    "target_account_key": build_account_key("live", "Broker-Live", 1001),
                    "target_account_alias": "main",
                    "strategy": "trend_alpha",
                    "symbol": "XAUUSD",
                    "timeframe": "M5",
                    "payload": template_payload,
                }
            ],
            "dead_lettered": [
                {
                    "intent_id": "intent-dead",
                    "intent_key": "sig-1:key2",
                    "signal_id": "sig-1",
                    "target_account_key": build_account_key("live", "Broker-Live", 1001),
                    "target_account_alias": "main",
                    "strategy": "trend_alpha",
                    "symbol": "XAUUSD",
                    "timeframe": "M5",
                    "payload": template_payload,
                    "last_error_code": "intent_attempts_exhausted",
                }
            ],
        }

    consumer._claim_fn = _claim_fn
    monkeypatch.setattr("src.trading.intents.consumer.time.sleep", lambda *_args, **_kwargs: None)

    consumer._worker()

    assert calls["count"] == 1
    assert [event.type for event in received] == ["intent_reclaimed", "intent_dead_lettered"]
    assert received[0].trace_id == "trace-intent-1"
    assert received[0].payload["claimed_by_instance_id"] == "executor-live-main"
    assert received[1].payload["status"] == "dead_lettered"
    assert received[1].payload["last_error_code"] == "intent_attempts_exhausted"


def test_execution_intent_consumer_worker_emits_claimed_event_with_trace_context(monkeypatch):
    received = []
    pipeline_bus = PipelineEventBus()
    pipeline_bus.add_listener(received.append)
    completed = []
    template_payload = {
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "strategy": "trend_alpha",
        "direction": "buy",
        "confidence": 0.73,
        "signal_state": "confirmed_buy",
        "scope": "confirmed",
        "indicators": {"atr14": {"atr": 3.2}},
        "metadata": {"signal_trace_id": "trace-claimed-1"},
        "generated_at": "2026-04-12T08:00:00+00:00",
        "signal_id": "sig-claimed-1",
        "reason": "test",
        "parent_bar_time": None,
    }

    class _Executor:
        def process_event(self, event: SignalEvent):
            return {"status": "ok", "signal_id": event.signal_id}

    consumer = ExecutionIntentConsumer(
        claim_fn=lambda **kwargs: [],
        complete_fn=lambda **kwargs: completed.append(kwargs),
        runtime_identity=_runtime_identity(instance_role="executor", live_topology_mode="multi_account"),
        trade_executor=_Executor(),
        pipeline_event_bus=pipeline_bus,
    )

    calls = {"count": 0}

    def _claim_fn(**kwargs):
        calls["count"] += 1
        if calls["count"] > 1:
            consumer._stop_event.set()
            return {"claimed": [], "reclaimed": [], "dead_lettered": []}
        return {
            "claimed": [
                {
                    "intent_id": "intent-claimed",
                    "intent_key": "sig-claimed-1:key",
                    "signal_id": "sig-claimed-1",
                    "target_account_key": build_account_key("live", "Broker-Live", 1001),
                    "target_account_alias": "main",
                    "strategy": "trend_alpha",
                    "symbol": "XAUUSD",
                    "timeframe": "M5",
                    "payload": template_payload,
                }
            ],
            "reclaimed": [],
            "dead_lettered": [],
        }

    consumer._claim_fn = _claim_fn
    monkeypatch.setattr("src.trading.intents.consumer.time.sleep", lambda *_args, **_kwargs: None)

    consumer._worker()

    assert calls["count"] == 2
    assert [event.type for event in received] == [
        PIPELINE_INTENT_CLAIMED,
        "execution_succeeded",
    ]
    assert received[0].trace_id == "trace-claimed-1"
    assert received[0].payload["trace_id"] == "trace-claimed-1"
    assert received[0].payload["target_account_alias"] == "main"
    assert received[0].payload["account_key"] == build_account_key("live", "Broker-Live", 1001)


def test_transport_canary_emits_published_claimed_and_execution_events(monkeypatch):
    received = []
    pipeline_bus = PipelineEventBus()
    pipeline_bus.add_listener(received.append)
    pending_items: list[dict] = []
    completed: list[dict] = []

    monkeypatch.setattr(
        "src.trading.intents.publisher.load_group_mt5_settings",
        lambda **kwargs: {
            "exec_a": MT5Settings(
                instance_name="live-exec-a",
                account_alias="exec_a",
                account_label="Exec A",
                mt5_login=1002,
                mt5_server="Broker-Live",
                mt5_path="C:/MT5/exec_a/terminal64.exe",
            )
        },
    )

    def _write_fn(batch):
        for row in batch:
            pending_items.append(
                {
                    "intent_id": row[1],
                    "intent_key": row[2],
                    "signal_id": row[3],
                    "target_account_key": row[4],
                    "target_account_alias": row[5],
                    "strategy": row[6],
                    "symbol": row[7],
                    "timeframe": row[8],
                    "payload": row[9],
                }
            )

    publisher = ExecutionIntentPublisher(
        write_fn=_write_fn,
        runtime_identity=_runtime_identity(
            instance_role="main",
            live_topology_mode="multi_account",
            account_alias="live_main",
        ),
        account_bindings={"exec_a": ["trend_alpha"]},
        strategy_deployments={
            "trend_alpha": StrategyDeployment(
                name="trend_alpha",
                status=StrategyDeploymentStatus.ACTIVE_GUARDED,
            )
        },
        auto_trade_enabled=True,
        pipeline_event_bus=pipeline_bus,
    )

    publisher.on_signal_event(_event())

    class _Executor:
        def process_event(self, event: SignalEvent):
            return {"status": "ok", "signal_id": event.signal_id}

    claim_calls = {"count": 0}

    def _claim_fn(**kwargs):
        claim_calls["count"] += 1
        if claim_calls["count"] > 1:
            consumer._stop_event.set()
            return {"claimed": [], "reclaimed": [], "dead_lettered": []}
        return {
            "claimed": list(pending_items),
            "reclaimed": [],
            "dead_lettered": [],
        }

    consumer = ExecutionIntentConsumer(
        claim_fn=_claim_fn,
        complete_fn=lambda **kwargs: completed.append(kwargs),
        runtime_identity=_runtime_identity(
            instance_role="executor",
            live_topology_mode="multi_account",
            instance_name="live-exec-a",
            account_alias="exec_a",
            login=1002,
        ),
        trade_executor=_Executor(),
        pipeline_event_bus=pipeline_bus,
    )

    monkeypatch.setattr(
        "src.trading.intents.consumer.time.sleep",
        lambda *_args, **_kwargs: None,
    )

    consumer._worker()

    assert [event.type for event in received] == [
        "intent_published",
        PIPELINE_INTENT_CLAIMED,
        "execution_succeeded",
    ]
    assert completed[0]["status"] == "completed"
    assert completed[0]["intent_id"] == pending_items[0]["intent_id"]
    assert received[1].payload["target_account_alias"] == "exec_a"
    assert received[2].payload["status"] == "completed"
