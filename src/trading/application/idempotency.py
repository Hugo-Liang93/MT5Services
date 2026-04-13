from __future__ import annotations

import json
from threading import RLock
from typing import Any, Optional

from .audit import TradeCommandAuditService


class TradeExecutionReplayService:
    """执行交易命令的幂等回放组件。

    只负责 `execute_trade` 成功结果的短期内存缓存与审计回放查询，
    不承担命令执行、审计写入或交易控制职责。
    """

    def __init__(
        self,
        audit_service: TradeCommandAuditService,
        *,
        memory_cache_limit: int = 500,
        prune_to_size: int = 250,
        audit_lookback_limit: int = 200,
    ) -> None:
        self._audit_service = audit_service
        self._memory_cache_limit = max(1, int(memory_cache_limit))
        self._prune_to_size = max(1, int(prune_to_size))
        self._audit_lookback_limit = max(1, int(audit_lookback_limit))
        self._lock = RLock()
        self._successful_trade_cache: dict[str, dict[str, Any]] = {}

    def cache_successful_trade_result(
        self,
        request_id: Optional[str],
        result: Any,
    ) -> None:
        normalized = self._normalize_request_id(request_id)
        if not normalized or not isinstance(result, dict):
            return
        with self._lock:
            self._successful_trade_cache[normalized] = dict(result)
            if len(self._successful_trade_cache) > self._memory_cache_limit:
                keys = list(self._successful_trade_cache.keys())
                for key in keys[:-self._prune_to_size]:
                    self._successful_trade_cache.pop(key, None)

    def find_successful_trade_result(self, request_id: str) -> Optional[dict[str, Any]]:
        normalized = self._normalize_request_id(request_id)
        if not normalized:
            return None
        with self._lock:
            cached = self._successful_trade_cache.get(normalized)
        if cached is not None:
            replayed = dict(cached)
            replayed["idempotent_replay"] = True
            replayed["idempotent_source"] = "memory"
            return replayed
        replayed = self._audit_service.fetch_successful_trade_result(
            request_id=normalized,
            limit=self._audit_lookback_limit,
        )
        if replayed is not None:
            self.cache_successful_trade_result(normalized, replayed)
            return replayed
        return None

    @staticmethod
    def _normalize_request_id(request_id: Optional[str]) -> str:
        return str(request_id or "").strip()


class TradeOperatorActionReplayConflictError(ValueError):
    """控制类动作的幂等键冲突。"""

    def __init__(
        self,
        message: str,
        *,
        command_type: str,
        idempotency_key: str,
        existing_record: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.command_type = command_type
        self.idempotency_key = idempotency_key
        self.existing_record = dict(existing_record or {})


class TradeOperatorActionReplayService:
    """控制类命令的幂等回放组件。

    负责基于 `command_type + idempotency_key` 做短期内存去重，并在进程重启后
    回退到命令审计中查找最近一次已记录结果。
    """

    def __init__(
        self,
        audit_service: TradeCommandAuditService,
        *,
        memory_cache_limit: int = 500,
        prune_to_size: int = 250,
        audit_lookback_limit: int = 200,
    ) -> None:
        self._audit_service = audit_service
        self._memory_cache_limit = max(1, int(memory_cache_limit))
        self._prune_to_size = max(1, int(prune_to_size))
        self._audit_lookback_limit = max(1, int(audit_lookback_limit))
        self._lock = RLock()
        self._recorded_action_cache: dict[str, dict[str, Any]] = {}

    def cache_recorded_action(
        self,
        *,
        command_type: str,
        request_payload: Optional[dict[str, Any]],
        response_payload: Optional[dict[str, Any]],
    ) -> None:
        normalized_command_type = self._normalize_command_type(command_type)
        normalized_idempotency_key = self._normalize_idempotency_key(
            (request_payload or {}).get("idempotency_key")
        )
        if (
            not normalized_command_type
            or not normalized_idempotency_key
            or not isinstance(response_payload, dict)
            or not response_payload
        ):
            return
        cache_key = self._cache_key(
            normalized_command_type,
            normalized_idempotency_key,
        )
        entry = {
            "request_fingerprint": self._request_fingerprint(request_payload),
            "request_payload": self._clone_dict(request_payload),
            "response_payload": self._clone_dict(response_payload),
        }
        with self._lock:
            self._recorded_action_cache[cache_key] = entry
            if len(self._recorded_action_cache) > self._memory_cache_limit:
                keys = list(self._recorded_action_cache.keys())
                for key in keys[:-self._prune_to_size]:
                    self._recorded_action_cache.pop(key, None)

    def find_recorded_action(
        self,
        *,
        command_type: str,
        idempotency_key: Optional[str],
        request_payload: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        normalized_command_type = self._normalize_command_type(command_type)
        normalized_idempotency_key = self._normalize_idempotency_key(idempotency_key)
        if not normalized_command_type or not normalized_idempotency_key:
            return None
        request_fingerprint = self._request_fingerprint(request_payload)
        cache_key = self._cache_key(
            normalized_command_type,
            normalized_idempotency_key,
        )
        with self._lock:
            cached_entry = self._recorded_action_cache.get(cache_key)
        if cached_entry is not None:
            return self._validate_entry(
                command_type=normalized_command_type,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint=request_fingerprint,
                entry=cached_entry,
                source="memory",
            )
        recorded = self._audit_service.fetch_recorded_operator_action(
            command_type=normalized_command_type,
            idempotency_key=normalized_idempotency_key,
            limit=self._audit_lookback_limit,
        )
        if recorded is None:
            return None
        if not isinstance(recorded.get("response_payload"), dict) or not recorded.get("response_payload"):
            return None
        replay_entry = {
            "request_fingerprint": self._request_fingerprint(
                recorded.get("request_payload") or {}
            ),
            "request_payload": self._clone_dict(recorded.get("request_payload")),
            "response_payload": self._clone_dict(recorded.get("response_payload")),
        }
        self.cache_recorded_action(
            command_type=normalized_command_type,
            request_payload=recorded.get("request_payload") or {},
            response_payload=recorded.get("response_payload") or {},
        )
        return self._validate_entry(
            command_type=normalized_command_type,
            idempotency_key=normalized_idempotency_key,
            request_fingerprint=request_fingerprint,
            entry=replay_entry,
            source="audit",
        )

    def _validate_entry(
        self,
        *,
        command_type: str,
        idempotency_key: str,
        request_fingerprint: str,
        entry: dict[str, Any],
        source: str,
    ) -> dict[str, Any]:
        existing_fingerprint = str(entry.get("request_fingerprint") or "")
        if existing_fingerprint and existing_fingerprint != request_fingerprint:
            response_payload = self._clone_dict(entry.get("response_payload"))
            raise TradeOperatorActionReplayConflictError(
                (
                    f"idempotency_key '{idempotency_key}' already used for "
                    f"a different {command_type} request"
                ),
                command_type=command_type,
                idempotency_key=idempotency_key,
                existing_record={
                    "action_id": response_payload.get("action_id"),
                    "audit_id": response_payload.get("audit_id"),
                    "recorded_at": response_payload.get("recorded_at"),
                    "request_payload": self._clone_dict(entry.get("request_payload")),
                    "response_payload": response_payload,
                },
            )
        return {
            "source": source,
            "response_payload": self._clone_dict(entry.get("response_payload")),
        }

    def _request_fingerprint(self, request_payload: Optional[dict[str, Any]]) -> str:
        normalized_payload = self._sanitize_payload(request_payload or {})
        return json.dumps(
            normalized_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def _sanitize_payload(self, value: Any) -> Any:
        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                if key_text in {"action_id", "audit_id", "account_alias"}:
                    continue
                normalized[key_text] = self._sanitize_payload(item)
            return normalized
        if isinstance(value, (list, tuple, set)):
            return [self._sanitize_payload(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    @staticmethod
    def _cache_key(command_type: str, idempotency_key: str) -> str:
        return f"{command_type}:{idempotency_key}"

    @staticmethod
    def _clone_dict(value: Any) -> dict[str, Any]:
        return dict(value or {}) if isinstance(value, dict) else {}

    @staticmethod
    def _normalize_command_type(command_type: Optional[str]) -> str:
        return str(command_type or "").strip()

    @staticmethod
    def _normalize_idempotency_key(idempotency_key: Optional[str]) -> str:
        return str(idempotency_key or "").strip()
