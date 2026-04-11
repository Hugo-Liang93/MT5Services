from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, Protocol

from ..metadata_keys import MetadataKey as MK
from ..models import SignalRecord

if TYPE_CHECKING:
    from src.persistence.db import TimescaleWriter


class SignalRepository(Protocol):
    def append(self, record: SignalRecord) -> None:
        ...

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
        ...

    def summary(self, *, hours: int = 24, scope: str = "confirmed") -> list[dict]:
        ...

    def fetch_winrates(
        self,
        *,
        hours: int = 168,
        symbol: Optional[str] = None,
    ) -> list[dict]:
        ...

    def fetch_expectancy_stats(
        self,
        *,
        hours: int = 168,
        symbol: Optional[str] = None,
    ) -> list[dict]:
        ...


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
        if signal_state.startswith("preview_") or signal_state.startswith("armed_") or signal_state == "cancelled":
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
            *[self._row_to_summary(row, scope="confirmed") for row in self._db.summarize_signal_events(hours=hours)],
            *[self._row_to_summary(row, scope="preview") for row in self._db.summarize_signal_preview_events(hours=hours)],
        ]

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
