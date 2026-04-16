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
        while not self._stop_event.is_set():
            transitions = self._claim_fn(
                target_account_key=self._runtime_identity.account_key,
                claimed_by_instance_id=self._runtime_identity.instance_id,
                claimed_by_run_id=self._runtime_identity.instance_id,
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
                self._emit_intent_event("intent_reclaimed", item)
            for item in dead_lettered:
                self._emit_intent_event(
                    "intent_dead_lettered",
                    item,
                    extra_payload={
                        "status": "dead_lettered",
                        "last_error_code": item.get("last_error_code"),
                    },
                )
            if not claimed:
                time.sleep(self._poll_interval_seconds)
                continue
            for item in claimed:
                if self._stop_event.is_set():
                    return
                self._emit_intent_event("intent_claimed", item)
                self._process_intent(item)

    def _process_intent(self, item: dict[str, Any]) -> None:
        intent_id = str(item.get("intent_id") or "")
        self._in_flight_intent_id = intent_id
        self._in_flight_since = time.monotonic()
        event: SignalEvent | None = None
        try:
            self._heartbeat(intent_id)
            event = signal_event_from_payload(dict(item.get("payload") or {}))
            event = self._with_intent_context(event, item)
            result = self._trade_executor.process_event(event)
            outcome = interpret_terminal_result(result)
            status = outcome.status
            self._total_processed += 1
            self._complete_fn(
                intent_id=intent_id,
                status=status,
                decision_metadata={
                    "claimed_by_instance_id": self._runtime_identity.instance_id,
                    "claimed_by_run_id": self._runtime_identity.instance_id,
                    "result": result,
                },
                last_error_code=outcome.error_code if status == "failed" else None,
            )
            emit_terminal_execution_event(
                pipeline_event_bus=self._pipeline_event_bus,
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
                    "claimed_by_run_id": self._runtime_identity.instance_id,
                    "instance_id": self._runtime_identity.instance_id,
                    "instance_role": self._runtime_identity.instance_role,
                    "source_metadata": dict(event.metadata or {}),
                },
            )
        except Exception as exc:
            logger.exception("Execution intent processing failed: %s", intent_id)
            self._total_processed += 1
            self._total_failed += 1
            self._complete_fn(
                intent_id=intent_id,
                status="failed",
                decision_metadata={
                    "claimed_by_instance_id": self._runtime_identity.instance_id,
                    "claimed_by_run_id": self._runtime_identity.instance_id,
                    "error": str(exc),
                },
                last_error_code=type(exc).__name__,
            )
            failed_event = event or self._fallback_event_for_item(item)
            emit_terminal_execution_event(
                pipeline_event_bus=self._pipeline_event_bus,
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
                    "claimed_by_run_id": self._runtime_identity.instance_id,
                    "instance_id": self._runtime_identity.instance_id,
                    "instance_role": self._runtime_identity.instance_role,
                },
            )
        finally:
            self._in_flight_intent_id = None
            self._in_flight_since = None

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
            claimed_by_run_id=self._runtime_identity.instance_id,
            lease_seconds=self._lease_seconds,
        )

    def _emit_intent_event(
        self,
        event_type: str,
        item: dict[str, Any],
        *,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        if self._pipeline_event_bus is None:
            return
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
            "claimed_by_run_id": self._runtime_identity.instance_id,
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
