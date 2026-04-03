from __future__ import annotations

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
