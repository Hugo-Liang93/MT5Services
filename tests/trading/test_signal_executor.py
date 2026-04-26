from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.monitoring.pipeline import (
    PIPELINE_ADMISSION_REPORT_APPENDED,
    PIPELINE_EXECUTION_BLOCKED,
    PIPELINE_EXECUTION_DECIDED,
    PIPELINE_EXECUTION_SKIPPED,
    PIPELINE_EXECUTION_SUBMITTED,
    PIPELINE_EXECUTION_SUCCEEDED,
    PipelineEventBus,
)
from src.signals.contracts import StrategyDeployment, StrategyDeploymentStatus
from src.signals.models import SignalEvent
from src.trading.execution.executor import ExecutorConfig, TradeExecutor
from src.trading.execution.gate import ExecutionGate, ExecutionGateConfig
from src.trading.execution.intrabar_guard import IntrabarTradeGuard
from src.trading.execution.pending_orders import inspect_pending_mt5_order
from src.trading.execution.reasons import (
    REASON_AUTO_TRADE_DISABLED,
    REASON_DUPLICATE_SIGNAL_ID,
    REASON_INTRABAR_POSITION_ALREADY_VALIDATED,
    REASON_INTRABAR_POSITION_HOLD,
    REASON_INTRABAR_SYNTHESIS_STALE,
    REASON_INTRABAR_SYNTHESIS_UNAVAILABLE,
    REASON_INTRABAR_TRADING_DISABLED,
    REASON_LIMIT_REACHED,
    REASON_QUOTE_STALE,
    REASON_STRATEGY_CANDIDATE_ONLY,
    REASON_STRATEGY_DEMO_VALIDATION,
    REASON_STRATEGY_LOCKED_SESSION,
    REASON_STRATEGY_LOCKED_TIMEFRAME,
    REASON_STRATEGY_MAX_LIVE_POSITIONS,
    REASON_STRATEGY_NOT_INTRABAR_ENABLED,
    REASON_STRATEGY_REQUIRES_PENDING_ENTRY,
    REASON_TRADE_PARAMS_UNAVAILABLE,
)
from src.trading.execution.sizing import TradeParameters


def _fire(executor: TradeExecutor, event: SignalEvent) -> None:
    """Send event and wait for async worker to process it."""
    executor.on_signal_event(event)
    executor.flush()


def _fire_intrabar(executor: TradeExecutor, event: SignalEvent) -> None:
    """Send intrabar event through local async adapter and wait for worker."""
    executor.on_intrabar_trade_signal(event)
    executor.flush()


def _find_last_event(received, event_type: str):
    for event in reversed(received):
        if event.type == event_type:
            return event
    raise AssertionError(
        f"event {event_type!r} not found in {[item.type for item in received]}"
    )


def _find_last_admission_with_reason(received, reason_code: str):
    for event in reversed(received):
        if event.type != PIPELINE_ADMISSION_REPORT_APPENDED:
            continue
        reasons = list(event.payload.get("reasons") or [])
        if any(reason.get("code") == reason_code for reason in reasons):
            return event
    raise AssertionError(
        f"admission report with reason {reason_code!r} not found in {[item.type for item in received]}"
    )


def _find_last_admission_with_reason_detail(
    received, reason_code: str, detail_key: str
):
    for event in reversed(received):
        if event.type != PIPELINE_ADMISSION_REPORT_APPENDED:
            continue
        reasons = list(event.payload.get("reasons") or [])
        for reason in reasons:
            details = dict(reason.get("details") or {})
            if reason.get("code") == reason_code and detail_key in details:
                return event, details
    raise AssertionError(
        f"admission report with reason {reason_code!r} and detail {detail_key!r} not found "
        f"in {[item.type for item in received]}"
    )


class DummyTradingModule:
    def __init__(self):
        self.calls = []
        self.live_positions = []
        self.live_orders = []

    def dispatch_operation(self, operation, payload):
        self.calls.append((operation, payload))
        return {"ticket": 1, "payload": payload}

    def account_info(self):
        return {"equity": 10000.0}

    def get_positions(self, symbol=None):
        if symbol is None:
            return list(self.live_positions)
        return [row for row in self.live_positions if row.get("symbol") == symbol]

    def get_orders(self, symbol=None):
        if symbol is None:
            return list(self.live_orders)
        return [row for row in self.live_orders if row.get("symbol") == symbol]


class DummyPositionManager:
    def __init__(self, positions):
        self._positions = positions
        self.track_calls = []

    def active_positions(self):
        return list(self._positions)

    def track_position(self, **kwargs):
        self.track_calls.append(kwargs)
        tracked = {
            "ticket": kwargs["ticket"],
            "symbol": kwargs["symbol"],
            "timeframe": kwargs.get("timeframe"),
            "strategy": kwargs.get("strategy"),
            "action": kwargs.get("action"),
        }
        self._positions.append(tracked)
        return tracked

    def is_after_eod_today(self) -> bool:
        return False


class DummyTradeOutcomeTracker:
    def __init__(self):
        self.opened = []

    def on_trade_opened(self, **kwargs):
        self.opened.append(kwargs)


class DummyPendingManager:
    def __init__(self, contexts=None, *, config=None):
        self._contexts = list(contexts or [])
        self.config = config
        self.tracked_orders = []

    def active_execution_contexts(self):
        return list(self._contexts)

    def status(self):
        return {
            "active_count": len(self._contexts),
            "entries": list(self._contexts),
            "stats": {},
        }

    def track_mt5_order(self, **kwargs):
        self.tracked_orders.append(kwargs)


def _build_event(
    *,
    spread_points: float,
    close_price: float,
    symbol_point: float = 0.01,
    market_structure: dict | None = None,
) -> SignalEvent:
    metadata = {
        "previous_state": "armed_buy",
        "spread_points": spread_points,
        "spread_price": spread_points * symbol_point,
        "symbol_point": symbol_point,
        "close_price": close_price,
        "signal_trace_id": "trace_sig_1",
    }
    if market_structure:
        metadata["market_structure"] = market_structure
    return SignalEvent(
        symbol="XAUUSD",
        timeframe="M5",
        strategy="sma_trend",
        direction="buy",
        confidence=0.9,
        signal_state="confirmed_buy",
        scope="confirmed",
        indicators={
            "atr14": {"atr": 2.0},
            "sma20": {"sma": close_price},
        },
        metadata=metadata,
        generated_at=datetime.now(timezone.utc),
        signal_id="sig_1",
        reason="test",
    )


def _build_intrabar_event(
    *,
    parent_bar_time: datetime | None = None,
    metadata_overrides: dict | None = None,
) -> SignalEvent:
    metadata = {
        "previous_state": "preview_buy",
        "signal_trace_id": "trace_intrabar_1",
    }
    if parent_bar_time is not None:
        metadata["intrabar_parent_bar_time"] = parent_bar_time
    if metadata_overrides:
        metadata.update(metadata_overrides)
    return SignalEvent(
        symbol="XAUUSD",
        timeframe="M5",
        strategy="sma_trend",
        direction="buy",
        confidence=0.8,
        signal_state="preview_buy",
        scope="intrabar",
        indicators={
            "atr14": {"atr": 2.0},
            "sma20": {"sma": 3000.0},
        },
        metadata=metadata,
        generated_at=datetime.now(timezone.utc),
        signal_id="sig_intrabar_1",
        reason="intrabar_test",
        parent_bar_time=parent_bar_time,
    )


def test_trade_executor_skips_when_spread_to_stop_ratio_is_too_high() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            sl_atr_multiplier=1.0,
            tp_atr_multiplier=2.0,
            max_spread_to_stop_ratio=0.2,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )

    # M5 SL = 2.0 ATR × 2.0 = 4.0 points → spread 120 / (4.0 × 100) = 0.30 > 0.2
    _fire(executor, _build_event(spread_points=120.0, close_price=3000.0))

    assert module.calls == []
    assert (
        executor.status()["recent_executions"][-1]["reason"]
        == "spread_to_stop_ratio_too_high"
    )


def test_trade_executor_records_cost_metrics_on_success() -> None:
    module = DummyTradingModule()
    pipeline_bus = PipelineEventBus()
    received = []
    pipeline_bus.add_listener(received.append)
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            sl_atr_multiplier=2.0,
            tp_atr_multiplier=4.0,
            max_spread_to_stop_ratio=0.5,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
        pipeline_event_bus=pipeline_bus,
    )

    _fire(executor, _build_event(spread_points=50.0, close_price=3000.0))

    assert module.calls
    cost = executor.status()["recent_executions"][-1]["cost"]
    assert cost["estimated_cost_points"] == 50.0
    assert cost["estimated_cost_price"] == 0.5
    assert cost["spread_to_stop_ratio"] is not None
    assert [event.type for event in received][-4:] == [
        PIPELINE_ADMISSION_REPORT_APPENDED,
        PIPELINE_EXECUTION_DECIDED,
        PIPELINE_EXECUTION_SUBMITTED,
        PIPELINE_EXECUTION_SUCCEEDED,
    ]
    assert received[-4].payload["decision"] == "allow"
    assert received[-4].payload["requested_operation"] == "signal_execution"
    assert received[-1].payload["status"] == "completed"


def test_trade_executor_blocks_when_quote_health_is_stale() -> None:
    module = DummyTradingModule()
    pipeline_bus = PipelineEventBus()
    received = []
    pipeline_bus.add_listener(received.append)
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            sl_atr_multiplier=2.0,
            tp_atr_multiplier=4.0,
            max_spread_to_stop_ratio=0.5,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
        pipeline_event_bus=pipeline_bus,
        quote_health_fn=lambda symbol: {
            "stale": symbol == "XAUUSD",
            "age_seconds": 5.2,
            "stale_threshold_seconds": 3.0,
        },
    )

    _fire(executor, _build_event(spread_points=20.0, close_price=3000.0))

    assert module.calls == []
    assert executor.last_risk_block == REASON_QUOTE_STALE
    assert executor.status()["recent_executions"][-1]["reason"] == REASON_QUOTE_STALE
    blocked = _find_last_event(received, PIPELINE_EXECUTION_BLOCKED)
    admission = _find_last_admission_with_reason(received, REASON_QUOTE_STALE)
    skipped = _find_last_event(received, PIPELINE_EXECUTION_SKIPPED)
    assert blocked.payload["reason"] == REASON_QUOTE_STALE
    assert admission.payload["decision"] == "block"
    assert admission.payload["stage"] == "market_tradability"
    assert admission.payload["reasons"][0]["code"] == REASON_QUOTE_STALE
    assert skipped.payload["skip_reason"] == REASON_QUOTE_STALE


def test_trade_executor_returns_structured_skip_when_auto_trade_is_disabled() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(enabled=False, min_confidence=0.5),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )

    result = executor.process_event(
        _build_event(spread_points=20.0, close_price=3000.0)
    )

    assert result is not None
    assert result["status"] == "skipped"
    assert result["reason"] == REASON_AUTO_TRADE_DISABLED
    assert executor.last_risk_block == REASON_AUTO_TRADE_DISABLED
    assert (
        executor.status()["recent_executions"][-1]["reason"]
        == REASON_AUTO_TRADE_DISABLED
    )
    assert module.calls == []


def test_trade_executor_returns_pre_trade_reason_for_confirmed_skip() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            strategy_deployments={
                "sma_trend": StrategyDeployment(
                    name="sma_trend",
                    status=StrategyDeploymentStatus.DEMO_VALIDATION,
                )
            },
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )

    result = executor.process_event(
        _build_event(spread_points=20.0, close_price=3000.0)
    )

    assert result is not None
    assert result["status"] == "skipped"
    assert result["reason"] == REASON_STRATEGY_DEMO_VALIDATION
    assert module.calls == []


class _StubRuntimeIdentity:
    """轻量 runtime_identity stub，供 §0dg P1 environment-aware deployment 测试。"""

    def __init__(self, environment: str) -> None:
        self.environment = environment


def test_pre_trade_demo_validation_passes_in_demo_environment() -> None:
    """§0dg P1 回归：pre_trade_checks 第二层 deployment 门禁必须按 environment 路由。

    旧实现无条件 `allows_live_execution()` → demo 环境路由的 demo_validation
    信号到达执行器仍被拒（与 §0dd 修了 publisher 但漏了 executor 同模式）。
    修复后：demo environment 下 demo_validation 应通过该门禁，不再返
    REASON_STRATEGY_DEMO_VALIDATION（后续可能因其他原因 skip，但绝不是
    deployment 拒绝）。
    """
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            strategy_deployments={
                "sma_trend": StrategyDeployment(
                    name="sma_trend",
                    status=StrategyDeploymentStatus.DEMO_VALIDATION,
                )
            },
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
        runtime_identity=_StubRuntimeIdentity(environment="demo"),
    )

    result = executor.process_event(
        _build_event(spread_points=20.0, close_price=3000.0)
    )

    # 关键断言：demo environment 下 deployment 门禁必须放行 demo_validation。
    # 若后续因其他原因 skip（execution_gate / pending_entry / ...），reason 一定
    # 不能是 REASON_STRATEGY_DEMO_VALIDATION。
    if result is not None and result.get("status") == "skipped":
        assert result["reason"] != REASON_STRATEGY_DEMO_VALIDATION, (
            f"demo environment 下 demo_validation 仍被 deployment 门禁拒绝 "
            f"(§0dg P1 回归)：{result}"
        )


def test_pre_trade_demo_validation_blocked_in_live_environment() -> None:
    """§0dg P1 反向锁：live environment 下 demo_validation 必须仍被拒（防 demo→live 漂移）。

    与 publisher 同口径（§0dd P1）：environment="live" 严格走
    `allows_live_execution()`，demo_validation 不在 active/active_guarded 集合
    内必拒。
    """
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            strategy_deployments={
                "sma_trend": StrategyDeployment(
                    name="sma_trend",
                    status=StrategyDeploymentStatus.DEMO_VALIDATION,
                )
            },
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
        runtime_identity=_StubRuntimeIdentity(environment="live"),
    )

    result = executor.process_event(
        _build_event(spread_points=20.0, close_price=3000.0)
    )

    assert result is not None
    assert result["status"] == "skipped"
    assert result["reason"] == REASON_STRATEGY_DEMO_VALIDATION
    assert module.calls == []


def test_pre_trade_deployment_gate_defaults_to_live_when_runtime_identity_missing() -> None:
    """§0dg P1：runtime_identity=None（如 fixture/legacy 调用）时，必须保守按
    live 口径门禁（严格 allows_live_execution()），与旧行为兼容。

    防止 stub 测试或脚本绕过 runtime_identity 让 demo_validation 误放行。
    """
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            strategy_deployments={
                "sma_trend": StrategyDeployment(
                    name="sma_trend",
                    status=StrategyDeploymentStatus.DEMO_VALIDATION,
                )
            },
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
        # 不传 runtime_identity（旧 stub 模式）
    )

    result = executor.process_event(
        _build_event(spread_points=20.0, close_price=3000.0)
    )

    assert result is not None
    assert result["status"] == "skipped"
    assert result["reason"] == REASON_STRATEGY_DEMO_VALIDATION


def test_trade_executor_duplicate_signal_id_uses_unified_reject_contract() -> None:
    module = DummyTradingModule()
    pipeline_bus = PipelineEventBus()
    received = []
    pipeline_bus.add_listener(received.append)
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(enabled=True, min_confidence=0.5),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
        pipeline_event_bus=pipeline_bus,
    )
    executor.executed_signal_ids.append("sig_1")

    result = executor.process_event(
        _build_event(spread_points=20.0, close_price=3000.0)
    )

    assert result is not None
    assert result["status"] == "skipped"
    assert result["reason"] == REASON_DUPLICATE_SIGNAL_ID
    blocked = _find_last_event(received, PIPELINE_EXECUTION_BLOCKED)
    admission = _find_last_admission_with_reason(received, REASON_DUPLICATE_SIGNAL_ID)
    assert blocked.payload["reason"] == REASON_DUPLICATE_SIGNAL_ID
    assert admission.payload["decision"] == "block"
    assert admission.payload["reasons"][0]["code"] == REASON_DUPLICATE_SIGNAL_ID


def test_trade_executor_returns_trade_params_reason_for_confirmed_skip() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(enabled=True, min_confidence=0.5),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )
    event = replace(
        _build_event(spread_points=20.0, close_price=3000.0),
        indicators={"sma20": {"sma": 3000.0}},
    )

    result = executor.process_event(event)

    assert result is not None
    assert result["status"] == "skipped"
    assert result["reason"] == REASON_TRADE_PARAMS_UNAVAILABLE
    assert result["category"] == "trade_params"
    assert module.calls == []


def test_trade_executor_returns_cost_reason_for_confirmed_skip() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            sl_atr_multiplier=1.0,
            tp_atr_multiplier=2.0,
            max_spread_to_stop_ratio=0.2,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )

    result = executor.process_event(
        _build_event(spread_points=120.0, close_price=3000.0)
    )

    assert result is not None
    assert result["status"] == "skipped"
    assert result["reason"] == "spread_to_stop_ratio_too_high"
    assert result["category"] == "cost_guard"
    assert module.calls == []


def test_trade_executor_returns_failed_result_when_market_dispatch_raises() -> None:
    class _FailingTradingModule(DummyTradingModule):
        def dispatch_operation(self, operation, payload):
            raise RuntimeError("broker timeout")

    module = _FailingTradingModule()
    pipeline_bus = PipelineEventBus()
    received = []
    pipeline_bus.add_listener(received.append)
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            sl_atr_multiplier=2.0,
            tp_atr_multiplier=4.0,
            max_spread_to_stop_ratio=0.5,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
        pipeline_event_bus=pipeline_bus,
    )

    result = executor.process_event(
        _build_event(spread_points=20.0, close_price=3000.0)
    )

    assert result is not None
    assert result["status"] == "failed"
    assert result["reason"] == "broker timeout"
    assert result["category"] == "dispatch"
    assert result["error_code"] == "RuntimeError"
    assert [event.type for event in received] == [
        PIPELINE_ADMISSION_REPORT_APPENDED,
        PIPELINE_EXECUTION_DECIDED,
    ]


def test_trade_executor_forwards_market_structure_metadata_to_dispatch() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            sl_atr_multiplier=2.0,
            tp_atr_multiplier=4.0,
            max_spread_to_stop_ratio=0.5,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )

    _fire(
        executor,
        _build_event(
            spread_points=30.0,
            close_price=3000.0,
            market_structure={
                "current_session": "new_york",
                "sweep_confirmation_state": "bullish_sweep_confirmed_previous_day_low",
                "structure_bias": "bullish_sweep_confirmed",
            },
        ),
    )

    assert module.calls
    payload = module.calls[0][1]
    assert payload["metadata"]["signal"]["strategy"] == "sma_trend"
    assert payload["metadata"]["market_structure"]["sweep_confirmation_state"] == (
        "bullish_sweep_confirmed_previous_day_low"
    )


def test_trade_executor_skips_when_symbol_position_limit_is_reached() -> None:
    module = DummyTradingModule()
    pipeline_bus = PipelineEventBus()
    received = []
    pipeline_bus.add_listener(received.append)
    executor = TradeExecutor(
        trading_module=module,
        position_manager=DummyPositionManager(
            [{"symbol": "XAUUSD"}, {"symbol": "XAUUSD"}, {"symbol": "EURUSD"}]
        ),
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            max_concurrent_positions_per_symbol=2,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
        pipeline_event_bus=pipeline_bus,
    )

    _fire(executor, _build_event(spread_points=20.0, close_price=3000.0))

    assert module.calls == []
    assert executor.status()["recent_executions"][-1]["reason"] == (
        "max_concurrent_positions_per_symbol"
    )
    blocked = _find_last_event(received, PIPELINE_EXECUTION_BLOCKED)
    admission = _find_last_admission_with_reason(received, REASON_LIMIT_REACHED)
    skipped = _find_last_event(received, PIPELINE_EXECUTION_SKIPPED)
    assert blocked.payload["reason"] == REASON_LIMIT_REACHED
    assert admission.payload["decision"] == "block"
    assert admission.payload["requested_operation"] == "signal_execution"
    assert admission.payload["reasons"][0]["code"] == REASON_LIMIT_REACHED
    assert skipped.payload["skip_reason"] == REASON_LIMIT_REACHED


def test_trade_executor_emits_intrabar_blocked_admission_when_guard_missing() -> None:
    module = DummyTradingModule()
    pipeline_bus = PipelineEventBus()
    received = []
    pipeline_bus.add_listener(received.append)
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(enabled=True, min_confidence=0.5),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
        pipeline_event_bus=pipeline_bus,
    )

    executor.process_event(
        _build_intrabar_event(parent_bar_time=datetime.now(timezone.utc))
    )

    assert module.calls == []
    assert [event.type for event in received] == [PIPELINE_ADMISSION_REPORT_APPENDED]
    assert received[0].payload["decision"] == "block"
    assert received[0].payload["requested_operation"] == "intrabar_execution"
    assert received[0].payload["reasons"][0]["code"] == "intrabar_guard_missing"


def test_trade_executor_local_worker_emits_terminal_skip_for_confirmed() -> None:
    module = DummyTradingModule()
    pipeline_bus = PipelineEventBus()
    received = []
    pipeline_bus.add_listener(received.append)
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(enabled=False, min_confidence=0.5),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
        pipeline_event_bus=pipeline_bus,
    )

    _fire(executor, _build_event(spread_points=20.0, close_price=3000.0))

    skipped = _find_last_event(received, PIPELINE_EXECUTION_SKIPPED)
    assert skipped.payload["skip_reason"] == REASON_AUTO_TRADE_DISABLED
    assert skipped.payload["status"] == "skipped"
    assert skipped.payload["signal_id"] == "sig_1"


def test_trade_executor_local_worker_emits_terminal_skip_for_intrabar() -> None:
    module = DummyTradingModule()
    pipeline_bus = PipelineEventBus()
    received = []
    pipeline_bus.add_listener(received.append)
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(enabled=False, min_confidence=0.5),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
        pipeline_event_bus=pipeline_bus,
    )

    _fire_intrabar(
        executor,
        replace(
            _build_intrabar_event(parent_bar_time=datetime.now(timezone.utc)),
            signal_state="intrabar_armed_buy",
        ),
    )

    skipped = _find_last_event(received, PIPELINE_EXECUTION_SKIPPED)
    assert skipped.payload["skip_reason"] == REASON_AUTO_TRADE_DISABLED
    assert skipped.payload["signal_scope"] == "intrabar"


def test_trade_executor_blocks_intrabar_when_synthesis_metadata_is_missing() -> None:
    module = DummyTradingModule()
    pipeline_bus = PipelineEventBus()
    received = []
    pipeline_bus.add_listener(received.append)
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(enabled=True, min_confidence=0.5),
        execution_gate=ExecutionGate(
            ExecutionGateConfig(
                intrabar_trading_enabled=True,
                intrabar_enabled_strategies=frozenset({"sma_trend"}),
            )
        ),
        pipeline_event_bus=pipeline_bus,
    )
    executor.set_intrabar_guard(IntrabarTradeGuard())

    executor.process_event(
        _build_intrabar_event(parent_bar_time=datetime.now(timezone.utc))
    )

    assert module.calls == []
    assert executor.last_risk_block == REASON_INTRABAR_SYNTHESIS_UNAVAILABLE
    blocked = _find_last_event(received, PIPELINE_EXECUTION_BLOCKED)
    admission, _ = _find_last_admission_with_reason_detail(
        received,
        REASON_INTRABAR_SYNTHESIS_UNAVAILABLE,
        "intrabar_synthesis",
    )
    assert blocked.payload["reason"] == REASON_INTRABAR_SYNTHESIS_UNAVAILABLE
    assert (
        sum(1 for event in received if event.type == PIPELINE_ADMISSION_REPORT_APPENDED)
        == 1
    )
    assert admission.payload["reasons"][0]["code"] == (
        REASON_INTRABAR_SYNTHESIS_UNAVAILABLE
    )


def test_trade_executor_blocks_intrabar_when_synthesis_is_stale() -> None:
    module = DummyTradingModule()
    pipeline_bus = PipelineEventBus()
    received = []
    pipeline_bus.add_listener(received.append)
    parent_bar_time = datetime.now(timezone.utc)
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(enabled=True, min_confidence=0.5),
        execution_gate=ExecutionGate(
            ExecutionGateConfig(
                intrabar_trading_enabled=True,
                intrabar_enabled_strategies=frozenset({"sma_trend"}),
            )
        ),
        pipeline_event_bus=pipeline_bus,
    )
    executor.set_intrabar_guard(IntrabarTradeGuard())

    executor.process_event(
        _build_intrabar_event(
            parent_bar_time=parent_bar_time,
            metadata_overrides={
                "intrabar_synthesis": {
                    "trigger_tf": "M1",
                    "synthesized_at": "2026-04-13T00:00:00+00:00",
                    "stale_threshold_seconds": 180.0,
                    "expected_interval_seconds": 60.0,
                    "last_child_bar_time": "2026-04-13T00:00:00+00:00",
                    "child_bar_count": 3,
                    "count": 9,
                }
            },
        )
    )

    assert module.calls == []
    assert executor.last_risk_block == REASON_INTRABAR_SYNTHESIS_STALE
    blocked = _find_last_event(received, PIPELINE_EXECUTION_BLOCKED)
    admission, details = _find_last_admission_with_reason_detail(
        received,
        REASON_INTRABAR_SYNTHESIS_STALE,
        "intrabar_synthesis",
    )
    assert blocked.payload["reason"] == REASON_INTRABAR_SYNTHESIS_STALE
    assert (
        sum(1 for event in received if event.type == PIPELINE_ADMISSION_REPORT_APPENDED)
        == 1
    )
    assert admission.payload["reasons"][0]["code"] == (REASON_INTRABAR_SYNTHESIS_STALE)
    details = details["intrabar_synthesis"]
    assert details["trigger_tf"] == "M1"
    assert details["stale"] is True


def test_trade_executor_process_event_returns_structured_skip_when_intrabar_is_blocked() -> (
    None
):
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(enabled=True, min_confidence=0.5),
        execution_gate=ExecutionGate(
            ExecutionGateConfig(
                intrabar_trading_enabled=True,
                intrabar_enabled_strategies=frozenset({"sma_trend"}),
            )
        ),
    )
    executor.set_intrabar_guard(IntrabarTradeGuard())

    result = executor.process_event(
        _build_intrabar_event(parent_bar_time=datetime.now(timezone.utc))
    )

    assert result is not None
    assert result["status"] == "skipped"
    assert result["reason"] == REASON_INTRABAR_SYNTHESIS_UNAVAILABLE
    assert module.calls == []


def test_trade_executor_returns_structured_skip_when_intrabar_auto_trade_is_disabled() -> (
    None
):
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(enabled=False, min_confidence=0.5),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )

    result = executor.process_event(
        _build_intrabar_event(parent_bar_time=datetime.now(timezone.utc))
    )

    assert result is not None
    assert result["status"] == "skipped"
    assert result["reason"] == REASON_AUTO_TRADE_DISABLED
    assert executor.last_risk_block == REASON_AUTO_TRADE_DISABLED
    assert module.calls == []


def test_trade_executor_returns_trade_params_reason_for_intrabar_skip() -> None:
    module = DummyTradingModule()
    pipeline_bus = PipelineEventBus()
    received = []
    pipeline_bus.add_listener(received.append)
    parent_bar_time = datetime.now(timezone.utc)
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(enabled=True, min_confidence=0.5),
        execution_gate=ExecutionGate(
            ExecutionGateConfig(
                intrabar_trading_enabled=True,
                intrabar_enabled_strategies=frozenset({"sma_trend"}),
            )
        ),
        pipeline_event_bus=pipeline_bus,
    )
    executor.set_intrabar_guard(IntrabarTradeGuard())

    event = _build_intrabar_event(
        parent_bar_time=parent_bar_time,
        metadata_overrides={
            "intrabar_synthesis": {
                "trigger_tf": "M1",
                "synthesized_at": datetime.now(timezone.utc).isoformat(),
                "stale_threshold_seconds": 180.0,
                "expected_interval_seconds": 60.0,
                "last_child_bar_time": parent_bar_time.isoformat(),
                "child_bar_count": 1,
                "count": 1,
                "status": "healthy",
                "stale": False,
            }
        },
    )
    event = replace(event, indicators={"sma20": {"sma": 3000.0}})

    result = executor.process_event(event)

    assert result is not None
    assert result["status"] == "skipped"
    assert result["reason"] == REASON_TRADE_PARAMS_UNAVAILABLE
    admission = _find_last_admission_with_reason(
        received, REASON_TRADE_PARAMS_UNAVAILABLE
    )
    assert admission.payload["requested_operation"] == "intrabar_execution"
    assert module.calls == []


def test_trade_executor_returns_structured_skip_when_intrabar_position_is_already_validated() -> (
    None
):
    module = DummyTradingModule()
    guard = IntrabarTradeGuard()
    parent_bar_time = datetime.now(timezone.utc)
    guard.record_trade("XAUUSD", "M5", "sma_trend", "buy", parent_bar_time)
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(enabled=True, min_confidence=0.5),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )
    executor.set_intrabar_guard(guard)
    event = replace(
        _build_event(spread_points=20.0, close_price=3000.0),
        metadata={
            **_build_event(spread_points=20.0, close_price=3000.0).metadata,
            "bar_time": parent_bar_time,
        },
    )

    result = executor.process_event(event)

    assert result is not None
    assert result["status"] == "skipped"
    assert result["reason"] == REASON_INTRABAR_POSITION_ALREADY_VALIDATED
    assert module.calls == []


def test_trade_executor_returns_structured_skip_when_confirmed_hold_keeps_intrabar_position() -> (
    None
):
    module = DummyTradingModule()
    guard = IntrabarTradeGuard()
    parent_bar_time = datetime.now(timezone.utc)
    guard.record_trade("XAUUSD", "M5", "sma_trend", "buy", parent_bar_time)
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(enabled=True, min_confidence=0.5),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )
    executor.set_intrabar_guard(guard)
    base_event = _build_event(spread_points=20.0, close_price=3000.0)
    event = replace(
        base_event,
        direction="hold",
        signal_state="confirmed_hold",
        metadata={
            **base_event.metadata,
            "bar_time": parent_bar_time,
        },
    )

    result = executor.process_event(event)

    assert result is not None
    assert result["status"] == "skipped"
    assert result["reason"] == REASON_INTRABAR_POSITION_HOLD
    assert module.calls == []


def test_trade_executor_skips_when_same_strategy_direction_position_is_active() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        position_manager=DummyPositionManager(
            [
                {
                    "symbol": "XAUUSD",
                    "timeframe": "M5",
                    "strategy": "sma_trend",
                    "action": "buy",
                }
            ]
        ),
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            max_concurrent_positions_per_symbol=3,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )

    _fire(executor, _build_event(spread_points=20.0, close_price=3000.0))

    assert module.calls == []
    assert executor.status()["recent_executions"][-1]["reason"] == (
        "position_same_strategy_direction"
    )


def test_trade_executor_allows_reentry_after_same_strategy_direction_position_is_closed() -> (
    None
):
    module = DummyTradingModule()
    tracked_positions = [
        {
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "strategy": "sma_trend",
            "action": "buy",
        }
    ]
    executor = TradeExecutor(
        trading_module=module,
        position_manager=DummyPositionManager(tracked_positions),
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            max_concurrent_positions_per_symbol=3,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )

    _fire(executor, _build_event(spread_points=20.0, close_price=3000.0))
    tracked_positions.clear()
    event = SignalEvent(
        **{
            **_build_event(spread_points=20.0, close_price=3000.0).__dict__,
            "signal_id": "sig_2",
            "generated_at": datetime.now(timezone.utc),
        }
    )

    _fire(executor, event)

    assert len(module.calls) == 1
    assert module.calls[0][1]["request_id"] == "sig_2"


def test_trade_executor_skips_when_same_strategy_direction_mt5_pending_order_exists() -> (
    None
):
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        pending_entry_manager=DummyPendingManager(
            [
                {
                    "symbol": "XAUUSD",
                    "timeframe": "M5",
                    "strategy": "sma_trend",
                    "direction": "buy",
                    "source": "mt5_order",
                }
            ]
        ),
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )

    _fire(executor, _build_event(spread_points=20.0, close_price=3000.0))

    assert module.calls == []
    assert executor.status()["recent_executions"][-1]["reason"] == (
        "pending_entry_same_strategy_direction"
    )


def test_trade_executor_blocks_candidate_strategy_live_execution() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            strategy_deployments={
                "sma_trend": StrategyDeployment(
                    name="sma_trend",
                    status=StrategyDeploymentStatus.CANDIDATE,
                )
            },
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )

    _fire(executor, _build_event(spread_points=20.0, close_price=3000.0))

    assert module.calls == []
    assert executor.status()["recent_executions"][-1]["reason"] == (
        REASON_STRATEGY_CANDIDATE_ONLY
    )


def test_trade_executor_blocks_demo_validation_strategy_live_execution() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            strategy_deployments={
                "sma_trend": StrategyDeployment(
                    name="sma_trend",
                    status=StrategyDeploymentStatus.DEMO_VALIDATION,
                )
            },
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )

    _fire(executor, _build_event(spread_points=20.0, close_price=3000.0))

    assert module.calls == []
    assert executor.status()["recent_executions"][-1]["reason"] == (
        REASON_STRATEGY_DEMO_VALIDATION
    )


def test_trade_executor_enforces_guarded_strategy_pending_entry_requirement() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.4,
            strategy_deployments={
                "sma_trend": StrategyDeployment(
                    name="sma_trend",
                    status=StrategyDeploymentStatus.ACTIVE_GUARDED,
                    locked_timeframes=("M5",),
                    locked_sessions=("london",),
                    max_live_positions=1,
                    require_pending_entry=True,
                    paper_shadow_required=True,
                    robustness_tier="tf_specific",
                )
            },
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )

    event = _build_event(spread_points=20.0, close_price=3000.0)
    event = SignalEvent(
        **{
            **event.__dict__,
            "metadata": {
                **event.metadata,
                "session_buckets": ["london"],
            },
        }
    )

    _fire(executor, event)

    assert module.calls == []
    assert executor.status()["recent_executions"][-1]["reason"] == (
        REASON_STRATEGY_REQUIRES_PENDING_ENTRY
    )


def test_trade_executor_blocks_timeframe_outside_deployment_contract() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.4,
            strategy_deployments={
                "sma_trend": StrategyDeployment(
                    name="sma_trend",
                    status=StrategyDeploymentStatus.ACTIVE_GUARDED,
                    locked_timeframes=("M30",),
                    locked_sessions=("london",),
                    max_live_positions=1,
                    require_pending_entry=False,
                    paper_shadow_required=True,
                    robustness_tier="tf_specific",
                )
            },
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )
    event = _build_event(spread_points=20.0, close_price=3000.0)
    event = SignalEvent(**{**event.__dict__, "timeframe": "M5"})

    _fire(executor, event)

    assert module.calls == []
    assert executor.status()["recent_executions"][-1]["reason"] == (
        REASON_STRATEGY_LOCKED_TIMEFRAME
    )


def test_trade_executor_blocks_session_outside_deployment_contract() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.4,
            strategy_deployments={
                "sma_trend": StrategyDeployment(
                    name="sma_trend",
                    status=StrategyDeploymentStatus.ACTIVE_GUARDED,
                    locked_timeframes=("M5",),
                    locked_sessions=("london",),
                    max_live_positions=1,
                    require_pending_entry=False,
                    paper_shadow_required=True,
                    robustness_tier="tf_specific",
                )
            },
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )
    event = _build_event(spread_points=20.0, close_price=3000.0)
    event = SignalEvent(
        **{
            **event.__dict__,
            "metadata": {
                **event.metadata,
                "session_buckets": ["asia"],
            },
        }
    )

    _fire(executor, event)

    assert module.calls == []
    assert executor.status()["recent_executions"][-1]["reason"] == (
        REASON_STRATEGY_LOCKED_SESSION
    )


def test_trade_executor_enforces_guarded_strategy_live_position_cap() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        position_manager=DummyPositionManager(
            [
                {
                    "symbol": "XAUUSD",
                    "timeframe": "M5",
                    "strategy": "sma_trend",
                    "action": "buy",
                }
            ]
        ),
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.4,
            strategy_deployments={
                "sma_trend": StrategyDeployment(
                    name="sma_trend",
                    status=StrategyDeploymentStatus.ACTIVE_GUARDED,
                    locked_timeframes=("M5",),
                    locked_sessions=("london",),
                    max_live_positions=1,
                    require_pending_entry=False,
                    paper_shadow_required=True,
                    robustness_tier="tf_specific",
                )
            },
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )

    event = _build_event(spread_points=20.0, close_price=3000.0)
    event = SignalEvent(
        **{
            **event.__dict__,
            "metadata": {
                **event.metadata,
                "session_buckets": ["london"],
            },
        }
    )

    _fire(executor, event)

    assert module.calls == []
    assert executor.status()["recent_executions"][-1]["reason"] == (
        REASON_STRATEGY_MAX_LIVE_POSITIONS
    )


def test_trade_executor_pending_submission_sets_reentry_cooldown_anchor() -> None:
    from src.trading.pending import PendingEntryConfig

    module = DummyTradingModule()
    pending_manager = DummyPendingManager(
        config=PendingEntryConfig(cancel_on_new_signal=False),
    )
    executor = TradeExecutor(
        trading_module=module,
        pending_entry_manager=pending_manager,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            reentry_cooldown_bars=3,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )

    _limit_spec = {"entry_type": "limit", "entry_price": 3000.0, "entry_zone_atr": 0.3}
    first = _build_event(spread_points=20.0, close_price=3000.0)
    first = SignalEvent(
        **{
            **first.__dict__,
            "signal_id": "sig_pending_1",
            "metadata": {
                **first.metadata,
                "bar_time": datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
                "entry_spec": _limit_spec,
            },
        }
    )
    second = _build_event(spread_points=20.0, close_price=3000.0)
    second = SignalEvent(
        **{
            **second.__dict__,
            "signal_id": "sig_pending_2",
            "metadata": {
                **second.metadata,
                "bar_time": datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
                "entry_spec": _limit_spec,
            },
            "generated_at": datetime.now(timezone.utc),
        }
    )

    _fire(executor, first)
    _fire(executor, second)

    assert len(module.calls) == 1
    assert len(pending_manager.tracked_orders) == 1
    assert executor._last_entry_bar_time[("XAUUSD", "sma_trend", "buy")] == datetime(
        2026, 1, 1, 0, 0, tzinfo=timezone.utc
    )


def test_trade_executor_uses_timeframe_specific_sizing_profile() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            sl_atr_multiplier=1.5,
            tp_atr_multiplier=3.0,
            max_spread_to_stop_ratio=0.5,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )
    event = _build_event(spread_points=20.0, close_price=3000.0)
    event = SignalEvent(**{**event.__dict__, "timeframe": "M5"})

    _fire(executor, event)

    assert module.calls
    payload = module.calls[0][1]
    # M5: sl_atr_mult=1.8, tp_atr_mult=2.5, ATR=2.0
    # SL = 3000 - 1.8*2 = 2996.4, TP = 3000 + 2.5*2 = 3005.0
    assert payload["sl"] == pytest.approx(2996.4)
    assert payload["tp"] == pytest.approx(3005.0)


def test_trade_executor_passes_signal_id_as_request_id() -> None:
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )

    _fire(executor, _build_event(spread_points=10.0, close_price=3000.0))

    assert module.calls
    assert module.calls[0][1]["request_id"] == "sig_1"


def test_trade_executor_registers_filled_pending_mt5_order_immediately() -> None:
    module = DummyTradingModule()
    module.live_positions = [
        {
            "ticket": 101,
            "symbol": "XAUUSD",
            "comment": "M5:sma_trend:limit_rsig1",
            "price_open": 2999.5,
            "sl": 2995.5,
            "tp": 3007.5,
            "volume": 0.05,
            "time": datetime.now(timezone.utc),
        }
    ]
    position_manager = DummyPositionManager([])
    outcome_tracker = DummyTradeOutcomeTracker()
    executor = TradeExecutor(
        trading_module=module,
        position_manager=position_manager,
        trade_outcome_tracker=outcome_tracker,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )

    result = inspect_pending_mt5_order(
        executor,
        {
            "signal_id": "sig_1",
            "ticket": 7001,
            "symbol": "XAUUSD",
            "direction": "buy",
            "strategy": "sma_trend",
            "timeframe": "M5",
            "confidence": 0.9,
            "regime": "trend",
            "comment": "M5:sma_trend:limit_rsig1",
            "params": TradeParameters(
                entry_price=3000.0,
                stop_loss=2996.0,
                take_profit=3008.0,
                position_size=0.05,
                risk_reward_ratio=2.0,
                atr_value=2.0,
                sl_distance=4.0,
                tp_distance=8.0,
            ),
        },
    )

    assert result["status"] == "filled"
    assert result["ticket"] == 101
    assert len(position_manager.track_calls) == 1
    assert position_manager.track_calls[0]["ticket"] == 101
    assert position_manager.track_calls[0]["signal_id"] == "sig_1"
    assert outcome_tracker.opened[0]["signal_id"] == "sig_1"
    assert outcome_tracker.opened[0]["fill_price"] == pytest.approx(2999.5)


def test_trade_executor_matches_filled_pending_order_by_strategy_prefix_when_comment_differs() -> (
    None
):
    module = DummyTradingModule()
    module.live_positions = [
        {
            "ticket": 202,
            "symbol": "XAUUSD",
            "type": 0,
            "comment": "M30:htf_h4_pullback:limit",
            "price_open": 4649.15,
            "sl": 4583.65,
            "tp": 4731.02,
            "volume": 0.01,
            "time": datetime.now(timezone.utc),
        }
    ]
    position_manager = DummyPositionManager([])
    outcome_tracker = DummyTradeOutcomeTracker()
    executor = TradeExecutor(
        trading_module=module,
        position_manager=position_manager,
        trade_outcome_tracker=outcome_tracker,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )

    result = inspect_pending_mt5_order(
        executor,
        {
            "signal_id": "sig_h4_pullback",
            "ticket": 7002,
            "symbol": "XAUUSD",
            "direction": "buy",
            "strategy": "htf_h4_pullback",
            "timeframe": "M30",
            "confidence": 0.53,
            "comment": "M30:htf_h4_pullba_r7fab5734",
            "params": TradeParameters(
                entry_price=4661.06,
                stop_loss=4583.65,
                take_profit=4731.02,
                position_size=0.01,
                risk_reward_ratio=1.0,
                atr_value=29.77,
                sl_distance=77.41,
                tp_distance=69.96,
            ),
        },
    )

    assert result["status"] == "filled"
    assert result["ticket"] == 202
    assert len(position_manager.track_calls) == 1
    assert position_manager.track_calls[0]["signal_id"] == "sig_h4_pullback"
    assert outcome_tracker.opened[0]["signal_id"] == "sig_h4_pullback"


def test_execution_gate_blocks_intrabar_when_disabled() -> None:
    gate = ExecutionGate(ExecutionGateConfig(intrabar_trading_enabled=False))

    allowed, reason = gate.check_intrabar(
        _build_intrabar_event(parent_bar_time=datetime.now(timezone.utc))
    )

    assert not allowed
    assert reason == REASON_INTRABAR_TRADING_DISABLED


def test_execution_gate_blocks_intrabar_when_strategy_not_enabled() -> None:
    gate = ExecutionGate(
        ExecutionGateConfig(
            intrabar_trading_enabled=True,
            intrabar_enabled_strategies=frozenset({"structured_breakout_follow"}),
        )
    )

    allowed, reason = gate.check_intrabar(
        _build_intrabar_event(parent_bar_time=datetime.now(timezone.utc))
    )

    assert not allowed
    assert reason == REASON_STRATEGY_NOT_INTRABAR_ENABLED


def test_trade_executor_circuit_breaker_auto_resets() -> None:
    """熔断器在超过 circuit_auto_reset_minutes 后自动恢复。"""
    module = DummyTradingModule()
    executor = TradeExecutor(
        trading_module=module,
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            sl_atr_multiplier=2.0,
            tp_atr_multiplier=4.0,
            max_consecutive_failures=1,
            circuit_auto_reset_minutes=10,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )
    # 手动设置熔断状态
    executor._circuit_open = True
    executor._consecutive_failures = 3
    # 设置为 15 分钟前开路
    from datetime import timedelta

    executor._circuit_open_at = datetime.now(timezone.utc) - timedelta(minutes=15)

    _fire(executor, _build_event(spread_points=20.0, close_price=3000.0))

    # 自动恢复后应该执行
    assert module.calls
    assert executor._circuit_open is False


def test_trade_executor_uses_live_positions_when_tracking_state_is_stale() -> None:
    module = DummyTradingModule()
    module.live_positions = [{"symbol": "XAUUSD"}, {"symbol": "XAUUSD"}]
    executor = TradeExecutor(
        trading_module=module,
        position_manager=DummyPositionManager([]),
        config=ExecutorConfig(
            enabled=True,
            min_confidence=0.5,
            max_concurrent_positions_per_symbol=2,
        ),
        execution_gate=ExecutionGate(ExecutionGateConfig()),
    )

    _fire(executor, _build_event(spread_points=10.0, close_price=3000.0))

    assert module.calls == []
    assert executor.status()["recent_executions"][-1]["reason"] == (
        "max_concurrent_positions_per_symbol"
    )


# ── ExecutionGate 单元测试 ─────────────────────────────────────────────────


def test_execution_gate_allows_confirmed_signal() -> None:
    gate = ExecutionGate(ExecutionGateConfig())
    event = _build_event(spread_points=10.0, close_price=3000.0)

    allowed, reason = gate.check(event)

    assert allowed
    assert reason == ""
