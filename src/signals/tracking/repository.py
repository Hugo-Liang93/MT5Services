from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, Protocol

from ..metadata_keys import MetadataKey as MK
from ..models import SignalRecord

if TYPE_CHECKING:
    from src.persistence.db import TimescaleWriter


class SignalRepository(Protocol):
    def append(self, record: SignalRecord) -> None: ...

    def recent(
        self,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy: Optional[str] = None,
        direction: Optional[str] = None,
        scope: str = "confirmed",
        limit: int = 200,
    ) -> list[dict]: ...

    def summary(self, *, hours: int = 24, scope: str = "confirmed") -> list[dict]: ...

    def recent_page(
        self,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy: Optional[str] = None,
        direction: Optional[str] = None,
        status: Optional[str] = None,
        actionability: Optional[str] = None,
        scope: str = "confirmed",
        from_time: Optional[Any] = None,
        to_time: Optional[Any] = None,
        page: int = 1,
        page_size: int = 200,
        sort: str = "generated_at_desc",
    ) -> dict[str, Any]: ...

    def update_admission_result(
        self,
        *,
        signal_id: str,
        actionability: str,
        guard_reason_code: Optional[str],
        rank_source: str = "native",
    ) -> bool:
        """回写 executor admission 结果（actionable / hold / blocked）。

        priority 与 guard_category 由实现内部按 confidence 与 reason 自动计算，
        listener 仅传 actionability + guard_reason_code + rank_source。
        signal_id 不存在时返回 False（不抛）。
        """
        ...

    def fetch_winrates(
        self,
        *,
        hours: int = 168,
        symbol: Optional[str] = None,
    ) -> list[dict]: ...

    def fetch_expectancy_stats(
        self,
        *,
        hours: int = 168,
        symbol: Optional[str] = None,
    ) -> list[dict]: ...


class TimescaleSignalRepository:
    def __init__(
        self,
        db_writer: "TimescaleWriter",
        storage_writer: Any = None,
    ):
        self._db = getattr(db_writer, "signal_repo", db_writer)
        self._storage_writer = storage_writer

    def append(self, record: SignalRecord) -> None:
        scope = self._resolve_scope(record.metadata)
        if scope == "preview":
            # preview 信号是 L3 best-effort，走异步队列释放 PG 连接
            if self._storage_writer is not None:
                self._storage_writer.enqueue("signal_preview", record.to_row())
            else:
                # 无 StorageWriter 时改为同步写入（standalone/测试模式）
                self._db.write_signal_preview_events([record.to_row()])
            return
        self._db.write_signal_events([record.to_row()])

    @staticmethod
    def _resolve_scope(metadata: dict[str, Any]) -> str:
        scope = str(metadata.get(MK.SCOPE, "confirmed")).strip().lower()
        signal_state = str(metadata.get(MK.SIGNAL_STATE, "")).strip().lower()
        if signal_state.startswith("confirmed_"):
            return "confirmed"
        if (
            signal_state.startswith("preview_")
            or signal_state.startswith("armed_")
            or signal_state == "cancelled"
        ):
            return "preview"
        if scope in {"intrabar", "preview"}:
            return "preview"
        return "confirmed"

    @staticmethod
    def _normalize_scope(scope: str) -> str:
        normalized = str(scope or "confirmed").strip().lower()
        if normalized not in {"confirmed", "preview", "all"}:
            raise ValueError(f"unsupported signal scope: {scope}")
        return normalized

    @staticmethod
    def _row_to_event(row: tuple, *, scope: str) -> dict:
        metadata = row[10] or {}
        return {
            "generated_at": row[0].isoformat() if row[0] else None,
            "signal_id": row[1],
            "symbol": row[2],
            "timeframe": row[3],
            "strategy": row[4],
            "direction": row[5],
            "confidence": row[6],
            "reason": row[7],
            "used_indicators": row[8] or [],
            "indicators_snapshot": row[9] or {},
            "metadata": metadata,
            "signal_state": metadata.get(MK.SIGNAL_STATE),
            "scope": scope,
            # P9 Phase 1.5: admission 字段在 fetch_signal_events 路径下不带回（仅 11 列），
            # query_signal_events 路径已通过 _dict_row_to_event 输出。该方法用于 recent()
            # 的简单列表场景，admission 字段保持 None；如需 admission 排序请用 recent_page。
            "actionability": None,
            "guard_reason_code": None,
            "guard_category": None,
            "priority": None,
            "rank_source": None,
        }

    @staticmethod
    def _row_to_summary(row: tuple, *, scope: str) -> dict:
        return {
            "symbol": row[0],
            "timeframe": row[1],
            "strategy": row[2],
            "direction": row[3],
            "count": int(row[4] or 0),
            "avg_confidence": row[5],
            "last_seen_at": row[6].isoformat() if row[6] else None,
            "scope": scope,
        }

    def recent(
        self,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy: Optional[str] = None,
        direction: Optional[str] = None,
        scope: str = "confirmed",
        limit: int = 200,
    ) -> list[dict]:
        scope = self._normalize_scope(scope)
        if scope == "confirmed":
            rows = self._db.fetch_signal_events(
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy,
                direction=direction,
                limit=limit,
            )
            return [self._row_to_event(row, scope="confirmed") for row in rows]
        if scope == "preview":
            rows = self._db.fetch_signal_preview_events(
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy,
                direction=direction,
                limit=limit,
            )
            return [self._row_to_event(row, scope="preview") for row in rows]

        confirmed_rows = [
            self._row_to_event(row, scope="confirmed")
            for row in self._db.fetch_signal_events(
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy,
                direction=direction,
                limit=limit,
            )
        ]
        preview_rows = [
            self._row_to_event(row, scope="preview")
            for row in self._db.fetch_signal_preview_events(
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy,
                direction=direction,
                limit=limit,
            )
        ]
        merged = sorted(
            confirmed_rows + preview_rows,
            key=lambda item: item.get("generated_at") or "",
            reverse=True,
        )
        return merged[:limit]

    def summary(self, *, hours: int = 24, scope: str = "confirmed") -> list[dict]:
        scope = self._normalize_scope(scope)
        if scope == "confirmed":
            rows = self._db.summarize_signal_events(hours=hours)
            return [self._row_to_summary(row, scope="confirmed") for row in rows]
        if scope == "preview":
            rows = self._db.summarize_signal_preview_events(hours=hours)
            return [self._row_to_summary(row, scope="preview") for row in rows]
        return [
            *[
                self._row_to_summary(row, scope="confirmed")
                for row in self._db.summarize_signal_events(hours=hours)
            ],
            *[
                self._row_to_summary(row, scope="preview")
                for row in self._db.summarize_signal_preview_events(hours=hours)
            ],
        ]

    def recent_page(
        self,
        *,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        strategy: Optional[str] = None,
        direction: Optional[str] = None,
        status: Optional[str] = None,
        actionability: Optional[str] = None,
        scope: str = "confirmed",
        from_time: Optional[Any] = None,
        to_time: Optional[Any] = None,
        page: int = 1,
        page_size: int = 200,
        sort: str = "generated_at_desc",
    ) -> dict[str, Any]:
        scope = self._normalize_scope(scope)
        return self._db.query_signal_events(
            scope=scope,
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            direction=direction,
            status=status,
            actionability=actionability,
            from_time=from_time,
            to_time=to_time,
            page=page,
            page_size=page_size,
            sort=sort,
        )

    # P9 Phase 1.5: Admission writeback —————————————————————————————————

    # actionability → priority 系数（与 docs/design/quantx-data-freshness-tiering.md
    # §6.2 + plan §1.5.1 字段设计一致）：
    # - actionable: 满分（priority = confidence）
    # - hold:       半折（未到 executor / demo_validation）
    # - blocked:    一折（被 guard 拒）
    _PRIORITY_WEIGHTS: dict[str, float] = {
        "actionable": 1.0,
        "hold": 0.5,
        "blocked": 0.1,
    }

    # P9 bug #3 修复：signal 持久化（StorageWriter 异步队列）与 admission writeback
    # （PipelineEventBus 同步分发）存在 race —— executor 先发 intent_published，
    # 此时 signal 还在 storage queue 里没落 PG → fetch_signal_confidence 返回 None。
    # 用退避重试给 storage_writer 时间 flush；总耗时 ~150ms，listener 已在异常隔离
    # multicast 内运行，不影响其他 listener。
    _ADMISSION_RETRY_DELAYS_MS: tuple[int, ...] = (50, 100)

    def update_admission_result(
        self,
        *,
        signal_id: str,
        actionability: str,
        guard_reason_code: Optional[str],
        rank_source: str = "native",
    ) -> bool:
        import time

        from src.trading.execution.reasons import reason_category

        if actionability not in self._PRIORITY_WEIGHTS:
            raise ValueError(
                f"unsupported actionability: {actionability!r}; "
                f"expected one of {sorted(self._PRIORITY_WEIGHTS)}"
            )
        weight = self._PRIORITY_WEIGHTS[actionability]
        confidence = self._db.fetch_signal_confidence(signal_id=signal_id)
        # P9 bug #3: signal 持久化是异步的，给 storage_writer ~150ms flush 窗口
        for delay_ms in self._ADMISSION_RETRY_DELAYS_MS:
            if confidence is not None:
                break
            time.sleep(delay_ms / 1000.0)
            confidence = self._db.fetch_signal_confidence(signal_id=signal_id)
        if confidence is None:
            # 信号不存在或重试后仍未持久化 — 跳过，保持幂等
            return False
        priority = confidence * weight
        guard_category = (
            reason_category(guard_reason_code) if guard_reason_code else None
        )
        return self._db.update_signal_admission(
            signal_id=signal_id,
            actionability=actionability,
            guard_reason_code=guard_reason_code,
            guard_category=guard_category,
            priority=priority,
            rank_source=rank_source,
        )

    def fetch_winrates(
        self,
        *,
        hours: int = 168,
        symbol: Optional[str] = None,
    ) -> list[dict]:
        rows = self._db.fetch_winrates(hours=hours, symbol=symbol)
        return [
            {
                "strategy": row[0],
                "direction": row[1],
                "total": row[2],
                "wins": row[3],
                "win_rate": float(row[4]) if row[4] is not None else None,
                "avg_confidence": float(row[5]) if row[5] is not None else None,
                "avg_move": float(row[6]) if row[6] is not None else None,
            }
            for row in rows
        ]

    def fetch_expectancy_stats(
        self,
        *,
        hours: int = 168,
        symbol: Optional[str] = None,
    ) -> list[dict]:
        rows = self._db.fetch_expectancy_stats(hours=hours, symbol=symbol)
        return [
            {
                "strategy": row[0],
                "direction": row[1],
                "total": int(row[2] or 0),
                "wins": int(row[3] or 0),
                "losses": int(row[4] or 0),
                "win_rate": float(row[5]) if row[5] is not None else None,
                "avg_win_move": float(row[6]) if row[6] is not None else None,
                "avg_loss_move": float(row[7]) if row[7] is not None else None,
                "expectancy": float(row[8]) if row[8] is not None else None,
                "payoff_ratio": float(row[9]) if row[9] is not None else None,
            }
            for row in rows
        ]
