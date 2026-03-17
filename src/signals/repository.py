from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, Protocol

from .models import SignalRecord

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
        action: Optional[str] = None,
        scope: str = "confirmed",
        limit: int = 200,
    ) -> list[dict]:
        ...

    def summary(self, *, hours: int = 24, scope: str = "confirmed") -> list[dict]:
        ...


class TimescaleSignalRepository:
    def __init__(self, db_writer: "TimescaleWriter"):
        self.db_writer = db_writer

    def append(self, record: SignalRecord) -> None:
        scope = self._resolve_scope(record.metadata)
        if scope == "preview":
            self.db_writer.write_signal_preview_events([record.to_row()])
            return
        self.db_writer.write_signal_events([record.to_row()])

    @staticmethod
    def _resolve_scope(metadata: dict[str, Any]) -> str:
        scope = str(metadata.get("scope", "confirmed")).strip().lower()
        signal_state = str(metadata.get("signal_state", "")).strip().lower()
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
        return {
            "generated_at": row[0].isoformat() if row[0] else None,
            "signal_id": row[1],
            "symbol": row[2],
            "timeframe": row[3],
            "strategy": row[4],
            "action": row[5],
            "confidence": row[6],
            "reason": row[7],
            "used_indicators": row[8] or [],
            "indicators_snapshot": row[9] or {},
            "metadata": row[10] or {},
            "scope": scope,
        }

    @staticmethod
    def _row_to_summary(row: tuple, *, scope: str) -> dict:
        return {
            "symbol": row[0],
            "timeframe": row[1],
            "strategy": row[2],
            "action": row[3],
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
        action: Optional[str] = None,
        scope: str = "confirmed",
        limit: int = 200,
    ) -> list[dict]:
        scope = self._normalize_scope(scope)
        if scope == "confirmed":
            rows = self.db_writer.fetch_signal_events(
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy,
                action=action,
                limit=limit,
            )
            return [self._row_to_event(row, scope="confirmed") for row in rows]
        if scope == "preview":
            rows = self.db_writer.fetch_signal_preview_events(
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy,
                action=action,
                limit=limit,
            )
            return [self._row_to_event(row, scope="preview") for row in rows]

        confirmed_rows = [
            self._row_to_event(row, scope="confirmed")
            for row in self.db_writer.fetch_signal_events(
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy,
                action=action,
                limit=limit,
            )
        ]
        preview_rows = [
            self._row_to_event(row, scope="preview")
            for row in self.db_writer.fetch_signal_preview_events(
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy,
                action=action,
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
            rows = self.db_writer.summarize_signal_events(hours=hours)
            return [self._row_to_summary(row, scope="confirmed") for row in rows]
        if scope == "preview":
            rows = self.db_writer.summarize_signal_preview_events(hours=hours)
            return [self._row_to_summary(row, scope="preview") for row in rows]
        return [
            *[self._row_to_summary(row, scope="confirmed") for row in self.db_writer.summarize_signal_events(hours=hours)],
            *[self._row_to_summary(row, scope="preview") for row in self.db_writer.summarize_signal_preview_events(hours=hours)],
        ]
