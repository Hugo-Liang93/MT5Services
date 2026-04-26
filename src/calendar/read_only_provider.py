from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.calendar.economic_calendar.observability import get_updates
from src.calendar.economic_calendar.trade_guard import (
    get_merged_risk_windows,
    get_risk_windows,
    get_trade_guard,
)
from src.clients.economic_calendar import EconomicCalendarError, EconomicCalendarEvent


class ReadOnlyEconomicCalendarProvider:
    """供账户本地风控读取共享经济日历事实，不运行本地同步线程。"""

    _SUCCESS_STATES = {"ready", "ok", "completed", "idle"}

    def __init__(
        self,
        *,
        db_writer: Any,
        settings: Any,
        runtime_identity: Any | None = None,
    ) -> None:
        self.db = db_writer
        self.settings = settings
        self.market_impact_analyzer = None
        self._runtime_identity = runtime_identity
        self._worker = None

    def _ensure_worker_running(self) -> None:
        return None

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def _runtime_rows(self) -> list[dict[str, Any]]:
        # §0y P3：runtime_task_status PK 是 (instance_id, component, task_name)，
        # 旧实现只按 component + instance_role 查 → 多 main 实例同库时
        # freshness / error / provider_status 可能来自别的实例。必须用注入的
        # runtime_identity.instance_id 过滤；未注入时保留旧全局查询行为。
        instance_id = (
            getattr(self._runtime_identity, "instance_id", None)
            if self._runtime_identity is not None
            else None
        )
        rows = self.db.fetch_runtime_task_status(
            component="economic_calendar",
            instance_id=instance_id,
            instance_role="main",
        )
        normalized: list[dict[str, Any]] = []
        for row in rows:
            normalized.append(
                {
                    "task_name": row[1],
                    "updated_at": row[2],
                    "state": row[3],
                    "started_at": row[4],
                    "completed_at": row[5],
                    "next_run_at": row[6],
                    "duration_ms": row[7],
                    "success_count": row[8],
                    "failure_count": row[9],
                    "consecutive_failures": row[10],
                    "last_error": row[11],
                    "details": row[12] or {},
                }
            )
        return normalized

    def _latest_attempt(self) -> dict[str, Any] | None:
        rows = self._runtime_rows()
        return rows[0] if rows else None

    def _last_success_at(self) -> datetime | None:
        rows = self._runtime_rows()
        completed = [
            row["completed_at"]
            for row in rows
            if row["completed_at"] is not None
            and str(row.get("state") or "").lower() in self._SUCCESS_STATES
        ]
        if not completed:
            return None
        return max(completed)

    def _provider_status(self) -> dict[str, Any]:
        latest = self._latest_attempt()
        if latest is None:
            return {}
        details = latest.get("details") or {}
        provider_status = details.get("provider_status") or {}
        return provider_status if isinstance(provider_status, dict) else {}

    def _job_status(self) -> dict[str, Any]:
        jobs: dict[str, Any] = {}
        for row in self._runtime_rows():
            task_name = str(row.get("task_name") or "").strip()
            if not task_name or task_name in jobs:
                continue
            jobs[task_name] = {
                "state": row.get("state"),
                "updated_at": row["updated_at"].isoformat()
                if row.get("updated_at") is not None
                else None,
                "started_at": row["started_at"].isoformat()
                if row.get("started_at") is not None
                else None,
                "completed_at": row["completed_at"].isoformat()
                if row.get("completed_at") is not None
                else None,
                "next_run_at": row["next_run_at"].isoformat()
                if row.get("next_run_at") is not None
                else None,
                "duration_ms": str(row["duration_ms"])
                if row.get("duration_ms") is not None
                else None,
                "success_count": int(row.get("success_count") or 0),
                "failure_count": int(row.get("failure_count") or 0),
                "consecutive_failures": int(row.get("consecutive_failures") or 0),
                "last_error": row.get("last_error"),
                "details": dict(row.get("details") or {}),
            }
        return jobs

    def refresh(self, *args, **kwargs) -> Dict[str, Any]:
        raise EconomicCalendarError("executor economic calendar provider is read-only")

    def get_events(
        self,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int = 1000,
        sources: Optional[List[str]] = None,
        countries: Optional[List[str]] = None,
        currencies: Optional[List[str]] = None,
        session_buckets: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        importance_min: Optional[int] = None,
    ) -> List[EconomicCalendarEvent]:
        rows = self.db.fetch_economic_calendar(
            start_time,
            end_time,
            limit,
            sources=sources,
            countries=countries,
            currencies=currencies,
            session_buckets=session_buckets,
            statuses=statuses,
            importance_min=importance_min,
        )
        return [EconomicCalendarEvent.from_db_row(row) for row in rows]

    def get_upcoming(
        self,
        hours: int = 24,
        limit: int = 200,
        sources: Optional[List[str]] = None,
        countries: Optional[List[str]] = None,
        currencies: Optional[List[str]] = None,
        session_buckets: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        importance_min: Optional[int] = None,
    ) -> List[EconomicCalendarEvent]:
        start_time = datetime.now(timezone.utc)
        end_time = start_time + timedelta(hours=hours)
        return self.get_events(
            start_time,
            end_time,
            limit=limit,
            sources=sources,
            countries=countries,
            currencies=currencies,
            session_buckets=session_buckets,
            statuses=statuses,
            importance_min=importance_min,
        )

    def get_high_impact_events(
        self,
        hours: int = 24,
        limit: int = 200,
        sources: Optional[List[str]] = None,
        countries: Optional[List[str]] = None,
        currencies: Optional[List[str]] = None,
        session_buckets: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        importance_min: Optional[int] = None,
    ) -> List[EconomicCalendarEvent]:
        threshold = (
            importance_min
            if importance_min is not None
            else self.settings.high_importance_threshold
        )
        return self.get_upcoming(
            hours=hours,
            limit=limit,
            sources=sources,
            countries=countries,
            currencies=currencies,
            session_buckets=session_buckets,
            statuses=statuses,
            importance_min=threshold,
        )

    def get_curated_events(
        self,
        hours: int = 24,
        limit: int = 200,
        sources: Optional[List[str]] = None,
        countries: Optional[List[str]] = None,
        currencies: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        importance_min: Optional[int] = None,
        include_all_day: Optional[bool] = None,
    ) -> List[EconomicCalendarEvent]:
        effective_sources = (
            sources if sources is not None else (self.settings.curated_sources or None)
        )
        effective_countries = (
            countries
            if countries is not None
            else (self.settings.curated_countries or None)
        )
        effective_currencies = (
            currencies
            if currencies is not None
            else (self.settings.curated_currencies or None)
        )
        effective_statuses = (
            statuses if statuses is not None else (self.settings.curated_statuses or None)
        )
        effective_importance = (
            importance_min
            if importance_min is not None
            else self.settings.curated_importance_min
        )
        allow_all_day = (
            self.settings.curated_include_all_day
            if include_all_day is None
            else include_all_day
        )
        items = self.get_upcoming(
            hours=hours,
            limit=limit,
            sources=effective_sources,
            countries=effective_countries,
            currencies=effective_currencies,
            statuses=effective_statuses,
            importance_min=effective_importance,
        )
        if allow_all_day:
            return items
        return [item for item in items if not item.all_day]

    def get_risk_windows(self, **kwargs) -> List[Dict[str, Any]]:
        return get_risk_windows(self, **kwargs)

    def get_merged_risk_windows(self, **kwargs) -> List[Dict[str, Any]]:
        return get_merged_risk_windows(self, **kwargs)

    def get_trade_guard(
        self,
        symbol: str,
        at_time: Optional[datetime] = None,
        lookahead_minutes: int = 180,
        lookback_minutes: int = 0,
        limit: int = 500,
        importance_min: Optional[int] = None,
    ) -> Dict[str, Any]:
        return get_trade_guard(
            self,
            symbol=symbol,
            at_time=at_time,
            lookahead_minutes=lookahead_minutes,
            lookback_minutes=lookback_minutes,
            limit=limit,
            importance_min=importance_min,
        )

    def get_updates(
        self,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int = 500,
        event_uid: Optional[str] = None,
        snapshot_reasons: Optional[List[str]] = None,
        job_types: Optional[List[str]] = None,
    ) -> List[Dict[str, object]]:
        return get_updates(
            self,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            event_uid=event_uid,
            snapshot_reasons=snapshot_reasons,
            job_types=job_types,
        )

    def staleness_seconds(self) -> Optional[float]:
        last_refresh_at = self._last_success_at()
        if last_refresh_at is None:
            return float("inf")
        return max(
            0.0,
            (datetime.now(timezone.utc) - last_refresh_at).total_seconds(),
        )

    def is_warming_up(self) -> bool:
        return False

    def is_stale(self) -> bool:
        staleness = self.staleness_seconds()
        if staleness is None:
            return False
        return staleness > float(self.settings.stale_after_seconds)

    def health_state(self) -> str:
        if not self.settings.enabled:
            return "disabled"
        if self.is_stale():
            return "stale"
        failure_threshold = max(
            0,
            int(self.settings.trade_guard_provider_failure_threshold),
        )
        if failure_threshold > 0:
            for state in self._provider_status().values():
                if not bool(state.get("enabled", True)):
                    continue
                if int(state.get("consecutive_failures") or 0) >= failure_threshold:
                    return "degraded"
        return "ok"

    def stats(self) -> Dict[str, object]:
        latest_attempt = self._latest_attempt()
        staleness_seconds = self.staleness_seconds()
        provider_status = self._provider_status()
        return {
            "enabled": str(bool(self.settings.enabled)).lower(),
            "running": "false",
            "health_state": self.health_state(),
            "warming_up": "false",
            "local_timezone": self.settings.local_timezone,
            "refresh_interval_seconds": str(
                self.settings.near_term_refresh_interval_seconds
            ),
            "calendar_sync_interval_seconds": str(
                self.settings.calendar_sync_interval_seconds
            ),
            "near_term_refresh_interval_seconds": str(
                self.settings.near_term_refresh_interval_seconds
            ),
            "release_watch_interval_seconds": str(
                self.settings.release_watch_interval_seconds
            ),
            "near_term_window_hours": str(self.settings.near_term_window_hours),
            "release_watch_lookback_minutes": str(
                self.settings.release_watch_lookback_minutes
            ),
            "release_watch_lookahead_minutes": str(
                self.settings.release_watch_lookahead_minutes
            ),
            "bootstrap_started_at": None,
            "bootstrap_deadline_at": None,
            "last_refresh_at": (
                self._last_success_at().isoformat()
                if self._last_success_at() is not None
                else None
            ),
            "last_refresh_started_at": (
                latest_attempt["started_at"].isoformat()
                if latest_attempt is not None
                and latest_attempt.get("started_at") is not None
                else None
            ),
            "last_refresh_completed_at": (
                latest_attempt["completed_at"].isoformat()
                if latest_attempt is not None
                and latest_attempt.get("completed_at") is not None
                else None
            ),
            "last_refresh_error": (
                latest_attempt.get("last_error") if latest_attempt is not None else None
            ),
            "last_refresh_status": (
                latest_attempt.get("state") if latest_attempt is not None else None
            ),
            "refresh_in_progress": "false",
            "last_refresh_duration_ms": (
                str(latest_attempt["duration_ms"])
                if latest_attempt is not None
                and latest_attempt.get("duration_ms") is not None
                else None
            ),
            "consecutive_failures": str(
                int(latest_attempt.get("consecutive_failures") or 0)
                if latest_attempt is not None
                else 0
            ),
            "staleness_seconds": (
                str(staleness_seconds) if staleness_seconds is not None else None
            ),
            "stale": str(self.is_stale()).lower(),
            "default_countries": ",".join(self.settings.default_countries),
            "provider_status": provider_status,
            "job_status": self._job_status(),
        }
