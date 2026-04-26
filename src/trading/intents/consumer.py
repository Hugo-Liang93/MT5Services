from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from src.config.runtime_identity import RuntimeIdentity
from src.monitoring.pipeline.event_bus import PipelineEvent, PipelineEventBus
from src.signals.metadata_keys import MetadataKey as MK
from src.signals.models import SignalEvent
from src.trading.execution.eventing import (
    emit_terminal_execution_event,
    interpret_terminal_result,
)
from src.trading.intents.codec import signal_event_from_payload
from src.trading.runtime.lifecycle import OwnedThreadLifecycle

logger = logging.getLogger(__name__)


class ExecutionIntentConsumer:
    def __init__(
        self,
        *,
        claim_fn,
        complete_fn,
        heartbeat_fn=None,
        mark_dispatched_fn=None,
        runtime_identity: RuntimeIdentity,
        trade_executor,
        pipeline_event_bus: PipelineEventBus | None = None,
        poll_interval_seconds: float = 0.5,
        batch_size: int = 20,
        lease_seconds: int = 30,
        max_attempts: int = 5,
    ) -> None:
        self._claim_fn = claim_fn
        self._complete_fn = complete_fn
        self._heartbeat_fn = heartbeat_fn
        # §0dm P1：mark_dispatched_fn 在 process_event 前 atomic transition
        # claimed → dispatched，None 时退化为旧行为（仅 lease 过期防御）。
        # 装配层（factories/signals.py）必须传入，否则 at-most-once 不生效。
        self._mark_dispatched_fn = mark_dispatched_fn
        self._runtime_identity = runtime_identity
        self._trade_executor = trade_executor
        self._pipeline_event_bus = pipeline_event_bus
        self._poll_interval_seconds = max(0.1, float(poll_interval_seconds))
        self._batch_size = max(1, int(batch_size))
        self._lease_seconds = max(5, int(lease_seconds))
        self._max_attempts = max(1, int(max_attempts))
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lifecycle = OwnedThreadLifecycle(
            self,
            "_worker_thread",
            label="ExecutionIntentConsumer",
        )
        # 在途意图追踪：记录当前正在执行的 intent_id，用于崩溃诊断和
        # 监控端判断 consumer 是否卡在某个 intent 上。
        self._in_flight_intent_id: str | None = None
        self._in_flight_since: float | None = None
        self._total_processed: int = 0
        self._total_failed: int = 0

    def start(self) -> None:
        if self._lifecycle.is_running():
            return
        self._stop_event.clear()
        self._lifecycle.ensure_running(
            lambda: threading.Thread(
                target=self._worker,
                name="execution-intent-consumer",
                daemon=True,
            )
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._lifecycle.stop(self._stop_event, timeout=timeout)

    def is_running(self) -> bool:
        return self._lifecycle.is_running()

    def _worker(self) -> None:
        # §0di P1：与 OperatorCommandConsumer (§0cc) 同模式三层异常隔离——
        # 顶层循环 try/except 隔离单轮异常，防止一次 claim/emit/process 异常
        # 打死整个 consumer 线程；瞬时 DB/网络/总线故障下 lease 过期自然 retry。
        while not self._stop_event.is_set():
            try:
                self._worker_iteration()
            except Exception:
                logger.exception(
                    "ExecutionIntentConsumer: unexpected error in worker loop; "
                    "continuing after backoff",
                )
                self._stop_event.wait(self._poll_interval_seconds)

    def _worker_iteration(self) -> None:
        transitions = self._claim_fn(
            target_account_key=self._runtime_identity.account_key,
            claimed_by_instance_id=self._runtime_identity.instance_id,
            claimed_by_run_id=self._runtime_identity.run_id,
            limit=self._batch_size,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
        )
        if isinstance(transitions, dict):
            claimed = list(transitions.get("claimed") or [])
            reclaimed = list(transitions.get("reclaimed") or [])
            dead_lettered = list(transitions.get("dead_lettered") or [])
        else:
            claimed = list(transitions or [])
            reclaimed = []
            dead_lettered = []
        for item in reclaimed:
            self._safe_emit_intent_event("intent_reclaimed", item)
        for item in dead_lettered:
            self._safe_emit_intent_event(
                "intent_dead_lettered",
                item,
                extra_payload={
                    "status": "dead_lettered",
                    "last_error_code": item.get("last_error_code"),
                },
            )
        if not claimed:
            self._stop_event.wait(self._poll_interval_seconds)
            return
        for item in claimed:
            if self._stop_event.is_set():
                return
            # §0di P2：emit 失败不应阻断 process_event（pipeline bus 是观测侧
            # 旁路，业务执行不依赖它）；_safe_emit 把瞬时总线故障降级为日志。
            self._safe_emit_intent_event("intent_claimed", item)
            self._process_intent(item)

    def _process_intent(self, item: dict[str, Any]) -> None:
        intent_id = str(item.get("intent_id") or "")
        self._in_flight_intent_id = intent_id
        self._in_flight_since = time.monotonic()
        event: SignalEvent | None = None
        # §0di P1：success/failure 分支都用 _safe_complete + _safe_emit_terminal
        # 包装外部副作用调用——complete_fn 失败 → lease 过期自然 retry，emit
        # 失败 → 旁路降级，都不再传播异常打死 worker（与 §0cc OperatorCommand
        # consumer 同模式）。
        # §0dm P1：process_event 期间启动后台 heartbeat 续租线程——MT5 dispatch
        # / 风控 / 挂单处理可能 > lease_seconds (30s)；旧实现仅入口处一次心跳，
        # process 中途 lease 过期会被另一个 worker reclaim 并真实重复下单。
        heartbeat_stop = threading.Event()
        heartbeat_thread = self._start_heartbeat_renewer(intent_id, heartbeat_stop)
        try:
            self._heartbeat(intent_id)
            event = signal_event_from_payload(dict(item.get("payload") or {}))
            event = self._with_intent_context(event, item)
            # §0dm P1：claimed → dispatched atomic CAS。失败 = lease 已被
            # 其他 worker 抢走（owner 不匹配或 status 已变），必须放弃 dispatch
            # 避免真实重复下单。
            if self._mark_dispatched_fn is not None:
                transitioned = self._mark_dispatched_fn(
                    intent_id=intent_id,
                    claimed_by_instance_id=self._runtime_identity.instance_id,
                    claimed_by_run_id=self._runtime_identity.run_id,
                )
                if not transitioned:
                    logger.warning(
                        "ExecutionIntentConsumer: claimed→dispatched CAS failed "
                        "for intent_id=%s (lease taken over before dispatch); "
                        "abandoning dispatch to avoid duplicate order",
                        intent_id,
                    )
                    return
            result = self._trade_executor.process_event(event)
            outcome = interpret_terminal_result(result)
            status = outcome.status
            self._total_processed += 1
            self._safe_complete(
                intent_id=intent_id,
                status=status,
                claimed_by_instance_id=self._runtime_identity.instance_id,
                claimed_by_run_id=self._runtime_identity.run_id,
                decision_metadata={
                    "claimed_by_instance_id": self._runtime_identity.instance_id,
                    "claimed_by_run_id": self._runtime_identity.run_id,
                    "result": result,
                },
                last_error_code=outcome.error_code if status == "failed" else None,
            )
            self._safe_emit_terminal(
                event=event,
                result=result,
                extra_payload={
                    "intent_id": item.get("intent_id"),
                    "intent_key": item.get("intent_key"),
                    "target_account_key": item.get("target_account_key"),
                    "target_account_alias": item.get("target_account_alias"),
                    "action_id": item.get("action_id"),
                    "trace_id": self._trace_id_for_item(item),
                    "account_key": self._runtime_identity.account_key,
                    "account_alias": self._runtime_identity.account_alias,
                    "claimed_by_instance_id": self._runtime_identity.instance_id,
                    "claimed_by_run_id": self._runtime_identity.run_id,
                    "instance_id": self._runtime_identity.instance_id,
                    "instance_role": self._runtime_identity.instance_role,
                    "source_metadata": dict(event.metadata or {}),
                },
            )
        except Exception as exc:
            logger.exception("Execution intent processing failed: %s", intent_id)
            self._total_processed += 1
            self._total_failed += 1
            self._safe_complete(
                intent_id=intent_id,
                status="failed",
                claimed_by_instance_id=self._runtime_identity.instance_id,
                claimed_by_run_id=self._runtime_identity.run_id,
                decision_metadata={
                    "claimed_by_instance_id": self._runtime_identity.instance_id,
                    "claimed_by_run_id": self._runtime_identity.run_id,
                    "error": str(exc),
                },
                last_error_code=type(exc).__name__,
            )
            failed_event = event or self._fallback_event_for_item(item)
            self._safe_emit_terminal(
                event=failed_event,
                result={
                    "status": "failed",
                    "reason": str(exc),
                    "category": "dispatch",
                    "error_code": type(exc).__name__,
                    "details": {"error": str(exc)},
                },
                extra_payload={
                    "intent_id": item.get("intent_id"),
                    "intent_key": item.get("intent_key"),
                    "target_account_key": item.get("target_account_key"),
                    "target_account_alias": item.get("target_account_alias"),
                    "action_id": item.get("action_id"),
                    "trace_id": self._trace_id_for_item(item),
                    "account_key": self._runtime_identity.account_key,
                    "account_alias": self._runtime_identity.account_alias,
                    "claimed_by_instance_id": self._runtime_identity.instance_id,
                    "claimed_by_run_id": self._runtime_identity.run_id,
                    "instance_id": self._runtime_identity.instance_id,
                    "instance_role": self._runtime_identity.instance_role,
                },
            )
        finally:
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=2.0)
            self._in_flight_intent_id = None
            self._in_flight_since = None

    def _start_heartbeat_renewer(
        self, intent_id: str, stop_event: threading.Event
    ) -> threading.Thread | None:
        """§0dm P1：lease 期间每 lease_seconds/3 续租一次。

        旧实现只在入口处调一次 _heartbeat，process_event 真实下单 + 风控 +
        挂单处理期间 lease 自然过期 → 另一 worker 可 reclaim 并重复下单。
        续租线程让 lease 在 process 期间持续有效，process 完成 / 异常时
        finally 中 stop_event.set() 立即终止。

        无 heartbeat_fn 时不启动（仍保持入口一次心跳由 _heartbeat 负责）。
        """
        if self._heartbeat_fn is None:
            return None
        interval = max(1.0, float(self._lease_seconds) / 3.0)

        def _renewer() -> None:
            # 第一次入口已 heartbeat 过；从 interval 后开始续租
            while not stop_event.wait(interval):
                try:
                    self._heartbeat(intent_id)
                except Exception:
                    logger.exception(
                        "ExecutionIntentConsumer: heartbeat renewer failed for "
                        "intent_id=%s; lease will expire and worker may be "
                        "preempted",
                        intent_id,
                    )
                    return

        thread = threading.Thread(
            target=_renewer,
            name=f"intent-heartbeat-{intent_id[:8] if intent_id else 'unknown'}",
            daemon=True,
        )
        thread.start()
        return thread

    def _safe_complete(self, **kwargs: Any) -> None:
        """§0di P1 + §0dj P2：complete_fn 失败 → lease 过期自然 retry，不打死
        worker；返 False 说明 lease 已被其他 worker 接管，本次回写应丢弃。"""
        try:
            updated = self._complete_fn(**kwargs)
            if updated is False:
                logger.warning(
                    "ExecutionIntentConsumer: complete returned 0 rows for "
                    "intent_id=%s (lease already taken over by another worker)",
                    kwargs.get("intent_id"),
                )
        except Exception:
            logger.exception(
                "ExecutionIntentConsumer: complete_fn failed (intent_id=%s); "
                "command will retry on next claim cycle (lease will expire)",
                kwargs.get("intent_id"),
            )

    def _safe_emit_terminal(self, **kwargs: Any) -> None:
        """§0di P2：terminal pipeline emit 失败 → 旁路降级，不阻断业务。"""
        try:
            emit_terminal_execution_event(
                pipeline_event_bus=self._pipeline_event_bus,
                **kwargs,
            )
        except Exception:
            logger.exception(
                "ExecutionIntentConsumer: emit_terminal_execution_event failed; "
                "pipeline bus is an observation sideline, business execution unaffected",
            )

    def status(self) -> dict[str, Any]:
        """返回消费者运行状态，供监控使用。"""
        in_flight_duration: float | None = None
        if self._in_flight_since is not None:
            in_flight_duration = round(time.monotonic() - self._in_flight_since, 2)
        return {
            "running": self.is_running(),
            "total_processed": self._total_processed,
            "total_failed": self._total_failed,
            "in_flight_intent_id": self._in_flight_intent_id,
            "in_flight_duration_seconds": in_flight_duration,
            "poll_interval_seconds": self._poll_interval_seconds,
            "batch_size": self._batch_size,
            "lease_seconds": self._lease_seconds,
            "max_attempts": self._max_attempts,
        }

    def _heartbeat(self, intent_id: str) -> None:
        if self._heartbeat_fn is None:
            return
        self._heartbeat_fn(
            intent_id=intent_id,
            claimed_by_instance_id=self._runtime_identity.instance_id,
            claimed_by_run_id=self._runtime_identity.run_id,
            lease_seconds=self._lease_seconds,
        )

    def _safe_emit_intent_event(
        self,
        event_type: str,
        item: dict[str, Any],
        *,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        """§0di P2：claimed/reclaimed/dead_lettered 事件 emit 异常隔离。

        旧实现在 process_event 之前直接调 pipeline_event_bus.emit() → 总线
        瞬时故障会让 consumer 在进入业务执行前崩溃；pipeline bus 是观测旁路，
        emit 失败不应阻断真实交易。本 helper 把瞬时总线故障降级为日志。
        """
        if self._pipeline_event_bus is None:
            return
        try:
            payload_row = dict(item.get("payload") or {})
            metadata = dict(payload_row.get("metadata") or {})
            payload = {
                "signal_id": item.get("signal_id"),
                "intent_id": item.get("intent_id"),
                "intent_key": item.get("intent_key"),
                "strategy": item.get("strategy"),
                "direction": payload_row.get("direction"),
                "target_account_key": item.get("target_account_key"),
                "target_account_alias": item.get("target_account_alias"),
                "action_id": item.get("action_id"),
                "trace_id": self._trace_id_for_item(item),
                "account_key": self._runtime_identity.account_key,
                "account_alias": self._runtime_identity.account_alias,
                "claimed_by_instance_id": self._runtime_identity.instance_id,
                "claimed_by_run_id": self._runtime_identity.run_id,
                "instance_id": self._runtime_identity.instance_id,
                "instance_role": self._runtime_identity.instance_role,
                "signal_scope": payload_row.get("scope"),
                "source_metadata": metadata,
            }
            if extra_payload:
                payload.update(extra_payload)
            trace_id = self._trace_id_for_item(item)
            self._pipeline_event_bus.emit(
                PipelineEvent(
                    type=event_type,
                    trace_id=trace_id,
                    symbol=str(item.get("symbol") or payload_row.get("symbol") or ""),
                    timeframe=str(
                        item.get("timeframe") or payload_row.get("timeframe") or ""
                    ),
                    scope=str(payload_row.get("scope") or "confirmed"),
                    ts=datetime.now(timezone.utc).isoformat(),
                    payload=payload,
                )
            )
        except Exception:
            logger.exception(
                "ExecutionIntentConsumer: _safe_emit_intent_event(%s) failed; "
                "pipeline bus is an observation sideline, business execution unaffected",
                event_type,
            )

    @staticmethod
    def _with_intent_context(event: SignalEvent, item: dict[str, Any]) -> SignalEvent:
        metadata = dict(event.metadata or {})
        metadata.setdefault("intent_id", item.get("intent_id"))
        metadata.setdefault("intent_key", item.get("intent_key"))
        metadata.setdefault("target_account_key", item.get("target_account_key"))
        metadata.setdefault("target_account_alias", item.get("target_account_alias"))
        metadata.setdefault(
            MK.SIGNAL_TRACE_ID,
            ExecutionIntentConsumer._trace_id_for_item(item),
        )
        return SignalEvent(
            symbol=event.symbol,
            timeframe=event.timeframe,
            strategy=event.strategy,
            direction=event.direction,
            confidence=event.confidence,
            signal_state=event.signal_state,
            scope=event.scope,
            indicators=dict(event.indicators),
            metadata=metadata,
            generated_at=event.generated_at,
            signal_id=event.signal_id,
            reason=event.reason,
            parent_bar_time=event.parent_bar_time,
        )

    @staticmethod
    def _fallback_event_for_item(item: dict[str, Any]) -> SignalEvent:
        payload_row = dict(item.get("payload") or {})
        metadata = dict(payload_row.get("metadata") or {})
        metadata.setdefault("intent_id", item.get("intent_id"))
        metadata.setdefault("intent_key", item.get("intent_key"))
        metadata.setdefault("target_account_key", item.get("target_account_key"))
        metadata.setdefault("target_account_alias", item.get("target_account_alias"))
        metadata.setdefault(
            MK.SIGNAL_TRACE_ID,
            ExecutionIntentConsumer._trace_id_for_item(item),
        )
        return SignalEvent(
            symbol=str(item.get("symbol") or payload_row.get("symbol") or ""),
            timeframe=str(item.get("timeframe") or payload_row.get("timeframe") or ""),
            strategy=str(item.get("strategy") or payload_row.get("strategy") or ""),
            direction=str(payload_row.get("direction") or ""),
            confidence=0.0,
            signal_state=str(payload_row.get("signal_state") or ""),
            scope=str(payload_row.get("scope") or "confirmed"),
            indicators={},
            metadata=metadata,
            generated_at=datetime.now(timezone.utc),
            signal_id=str(item.get("signal_id") or payload_row.get("signal_id") or ""),
            reason=str(payload_row.get("reason") or ""),
            parent_bar_time=None,
        )

    @staticmethod
    def _trace_id_for_item(item: dict[str, Any]) -> str:
        payload_row = dict(item.get("payload") or {})
        metadata = dict(payload_row.get("metadata") or {})
        return str(
            metadata.get(MK.SIGNAL_TRACE_ID)
            or metadata.get("trace_id")
            or payload_row.get("trace_id")
            or item.get("trace_id")
            or item.get("signal_id")
            or item.get("intent_id")
            or ""
        ).strip()
