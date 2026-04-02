from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional


class RuntimeReadModel:
    """统一运行时读模型，供 admin、monitoring 与前端投影视图复用。"""

    def __init__(
        self,
        *,
        health_monitor: Any = None,
        ingestor: Any = None,
        indicator_manager: Any = None,
        trading_service: Any = None,
        signal_runtime: Any = None,
        trade_executor: Any = None,
        position_manager: Any = None,
        pending_entry_manager: Any = None,
        trading_state_store: Any = None,
        trading_state_alerts: Any = None,
    ) -> None:
        self._health_monitor = health_monitor
        self._ingestor = ingestor
        self._indicator_manager = indicator_manager
        self._trading_service = trading_service
        self._signal_runtime = signal_runtime
        self._trade_executor = trade_executor
        self._position_manager = position_manager
        self._pending_entry_manager = pending_entry_manager
        self._trading_state_store = trading_state_store
        self._trading_state_alerts = trading_state_alerts

    @staticmethod
    def _runtime_indicator_status(
        event_loop_running: bool,
        failed_computations: int,
        event_stats: Mapping[str, Any],
    ) -> str:
        if not event_loop_running:
            return "critical"
        if int(event_stats.get("failed", 0) or 0) > 0:
            return "critical"
        if failed_computations > 0 or int(event_stats.get("retrying", 0) or 0) > 0:
            return "warning"
        return "healthy"

    @staticmethod
    def build_storage_summary(queue_stats: Mapping[str, Any]) -> dict[str, Any]:
        summary = dict(queue_stats.get("summary", {}) or {})
        queues = dict(queue_stats.get("queues", {}) or {})
        threads = dict(queue_stats.get("threads", {}) or {})
        worst_queue = None

        for name, queue in queues.items():
            queue_status = str(queue.get("status", "normal"))
            queue_score = {"normal": 0, "high": 1, "critical": 2, "full": 3}.get(
                queue_status, 0
            )
            if worst_queue is None or queue_score > worst_queue["score"]:
                worst_queue = {
                    "name": name,
                    "score": queue_score,
                    "status": queue_status,
                    "utilization_pct": queue.get("utilization_pct", 0.0),
                    "pending": queue.get("pending", 0),
                }

        if not threads.get("writer_alive", False):
            status = "critical"
        elif int(summary.get("full", 0) or 0) > 0:
            status = "critical"
        elif int(summary.get("critical", 0) or 0) > 0 or int(
            summary.get("high", 0) or 0
        ) > 0:
            status = "warning"
        else:
            status = "healthy"

        return {
            "status": status,
            "threads": threads,
            "summary": summary,
            "worst_queue": worst_queue,
        }

    @classmethod
    def build_indicator_summary(
        cls, indicator_stats: Mapping[str, Any]
    ) -> dict[str, Any]:
        event_stats = dict(indicator_stats.get("event_store", {}) or {})
        pipeline_stats = dict(indicator_stats.get("pipeline", {}) or {})
        event_loop_running = bool(indicator_stats.get("event_loop_running", False))
        failed_computations = int(indicator_stats.get("failed_computations", 0) or 0)

        return {
            "status": cls._runtime_indicator_status(
                event_loop_running,
                failed_computations,
                event_stats,
            ),
            "mode": indicator_stats.get("mode"),
            "event_loop_running": event_loop_running,
            "last_reconcile_at": indicator_stats.get("last_reconcile_at"),
            "computations": {
                "total": int(indicator_stats.get("total_computations", 0) or 0),
                "failed": failed_computations,
                "success_rate": indicator_stats.get("success_rate", 0),
                "cached": int(indicator_stats.get("cached_computations", 0) or 0),
                "incremental": int(
                    indicator_stats.get("incremental_computations", 0) or 0
                ),
                "parallel": int(indicator_stats.get("parallel_computations", 0) or 0),
            },
            "events": {
                "pending": int(event_stats.get("pending", 0) or 0),
                "processing": int(event_stats.get("processing", 0) or 0),
                "completed": int(event_stats.get("completed", 0) or 0),
                "skipped": int(event_stats.get("skipped", 0) or 0),
                "failed": int(event_stats.get("failed", 0) or 0),
                "retrying": int(event_stats.get("retrying", 0) or 0),
                "total_retries": int(event_stats.get("total_retries", 0) or 0),
                "outcome_counts": dict(event_stats.get("outcome_counts", {}) or {}),
                "recent_skips": list(event_stats.get("recent_skips", []) or []),
                "recent_retryable_errors": list(
                    event_stats.get("recent_retryable_errors", []) or []
                ),
                "recent_errors": list(event_stats.get("recent_errors", []) or []),
            },
            "cache": {
                "hits": int(indicator_stats.get("cache_hits", 0) or 0),
                "misses": int(indicator_stats.get("cache_misses", 0) or 0),
                "snapshot": dict(pipeline_stats.get("cache", {}) or {}),
            },
            "results": dict(indicator_stats.get("results", {}) or {}),
            "config": dict(indicator_stats.get("config", {}) or {}),
            "timestamp": indicator_stats.get("timestamp"),
        }

    @staticmethod
    def build_trading_summary(trading_stats: Mapping[str, Any]) -> dict[str, Any]:
        summary_rows = list(trading_stats.get("summary", []) or [])
        accounts = list(trading_stats.get("accounts", []) or [])
        recent = list(trading_stats.get("recent", []) or [])
        active_account_alias = trading_stats.get("active_account_alias")
        failed = sum(
            int(row.get("count", 0) or 0)
            for row in summary_rows
            if row.get("status") == "failed"
        )
        status = "warning" if failed > 0 else "healthy"
        daily = dict(trading_stats.get("daily", {}) or {})
        risk_summary = dict(daily.get("risk", {}) or {})

        coordination_issues: list[str] = []
        if int(daily.get("failed", 0) or 0) > 0 and int(
            daily.get("success", 0) or 0
        ) == 0:
            coordination_issues.append(
                "当日交易全部失败，建议检查风控结果与交易连接状态。"
            )
        if failed > 0:
            coordination_issues.append(
                "检测到失败交易记录，建议检查交易调度、账户状态与执行日志。"
            )
        if int(risk_summary.get("blocked", 0) or 0) > 0:
            coordination_issues.append(
                "存在风控拦截交易，建议复核风控阈值、经济事件窗口与仓位限制。"
            )

        return {
            "status": status,
            "active_account_alias": active_account_alias,
            "accounts": accounts,
            "daily": daily,
            "risk": risk_summary,
            "coordination_issues": coordination_issues,
            "summary": summary_rows,
            "recent": recent,
        }

    @staticmethod
    def build_system_status(startup_status: Mapping[str, Any]) -> dict[str, Any]:
        started_at = startup_status.get("started_at")
        completed_at = startup_status.get("completed_at")
        uptime: Optional[float] = None
        if completed_at:
            try:
                ready_at = datetime.fromisoformat(str(completed_at))
                uptime = (datetime.now(timezone.utc) - ready_at).total_seconds()
            except (TypeError, ValueError):
                uptime = None

        return {
            "status": "healthy" if startup_status.get("ready") else "starting",
            "uptime_seconds": uptime,
            "started_at": str(started_at) if started_at else None,
            "ready": bool(startup_status.get("ready")),
            "phase": str(startup_status.get("phase", "unknown")),
        }

    @staticmethod
    def build_pending_entries_summary(
        pending_status: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        payload = dict(pending_status or {})
        entries = list(payload.get("entries", []) or [])
        stats = dict(payload.get("stats", {}) or {})
        return {
            "status": "healthy",
            "active_count": int(payload.get("active_count", len(entries)) or 0),
            "entries": entries,
            "stats": stats,
        }

    @classmethod
    def build_executor_snapshot(cls, executor_status: Mapping[str, Any]) -> dict[str, Any]:
        circuit_breaker = dict(executor_status.get("circuit_breaker", {}) or {})
        pending_entries = cls.build_pending_entries_summary(
            executor_status.get("pending_entries", {})
        )
        circuit_open = bool(
            circuit_breaker.get("open", executor_status.get("circuit_open", False))
        )
        consecutive_failures = int(
            circuit_breaker.get(
                "consecutive_failures",
                executor_status.get("consecutive_failures", 0),
            )
            or 0
        )
        enabled = bool(executor_status.get("enabled", False))
        last_error = executor_status.get("last_error")

        return {
            "status": (
                "disabled"
                if not enabled
                else "warning"
                if circuit_open or last_error
                else "healthy"
            ),
            "enabled": enabled,
            "circuit_open": circuit_open,
            "consecutive_failures": consecutive_failures,
            "execution_count": int(executor_status.get("execution_count", 0) or 0),
            "last_execution_at": executor_status.get("last_execution_at"),
            "last_error": last_error,
            "last_risk_block": executor_status.get("last_risk_block"),
            "signals": {
                "received": int(executor_status.get("signals_received", 0) or 0),
                "passed": int(executor_status.get("signals_passed", 0) or 0),
                "blocked": int(executor_status.get("signals_blocked", 0) or 0),
                "skip_reasons": dict(executor_status.get("skip_reasons", {}) or {}),
                "by_timeframe": dict(executor_status.get("by_timeframe", {}) or {}),
            },
            "execution_quality": dict(
                executor_status.get("execution_quality", {}) or {}
            ),
            "config": dict(executor_status.get("config", {}) or {}),
            "execution_gate": dict(executor_status.get("execution_gate", {}) or {}),
            "pending_entries_count": pending_entries["active_count"],
            "pending_entries": pending_entries,
            "recent_executions": list(executor_status.get("recent_executions", []) or []),
        }

    @staticmethod
    def build_positions_overview(
        positions: list[dict[str, Any]], *, limit: int = 20
    ) -> dict[str, Any]:
        return {"count": len(positions), "items": positions[:limit]}

    @staticmethod
    def build_signal_runtime_summary(
        runtime_status: Mapping[str, Any],
    ) -> dict[str, Any]:
        running = bool(runtime_status.get("running", False))
        last_error = runtime_status.get("last_error")
        dropped_events = int(runtime_status.get("dropped_events", 0) or 0)
        backpressure_failures = int(
            runtime_status.get("confirmed_backpressure_failures", 0) or 0
        )
        if not running:
            status = "critical"
        elif last_error or backpressure_failures > 0 or dropped_events > 0:
            status = "warning"
        else:
            status = "healthy"

        confirmed_size = int(runtime_status.get("confirmed_queue_size", 0) or 0)
        intrabar_size = int(runtime_status.get("intrabar_queue_size", 0) or 0)
        confirmed_capacity = int(
            runtime_status.get("confirmed_queue_capacity", 0) or 0
        )
        intrabar_capacity = int(runtime_status.get("intrabar_queue_capacity", 0) or 0)

        return {
            "status": status,
            "running": running,
            "target_count": int(runtime_status.get("target_count", 0) or 0),
            "trigger_mode": dict(runtime_status.get("trigger_mode", {}) or {}),
            "strategy_sessions": dict(
                runtime_status.get("strategy_sessions", {}) or {}
            ),
            "strategy_scopes": dict(runtime_status.get("strategy_scopes", {}) or {}),
            "market_structure": {
                "enabled": bool(runtime_status.get("market_structure_enabled", False)),
                "cache_entries": int(
                    runtime_status.get("market_structure_cache_entries", 0) or 0
                ),
            },
            "queues": {
                "confirmed": {
                    "size": confirmed_size,
                    "capacity": confirmed_capacity,
                },
                "intrabar": {
                    "size": intrabar_size,
                    "capacity": intrabar_capacity,
                },
                "total": confirmed_size + intrabar_size,
                "total_capacity": confirmed_capacity + intrabar_capacity,
                "confirmed_backpressure_waits": int(
                    runtime_status.get("confirmed_backpressure_waits", 0) or 0
                ),
                "confirmed_backpressure_failures": backpressure_failures,
            },
            "processing": {
                "run_count": int(runtime_status.get("run_count", 0) or 0),
                "processed_events": int(
                    runtime_status.get("processed_events", 0) or 0
                ),
                "dropped_events": dropped_events,
                "dropped_confirmed": int(
                    runtime_status.get("dropped_confirmed", 0) or 0
                ),
                "dropped_intrabar": int(
                    runtime_status.get("dropped_intrabar", 0) or 0
                ),
            },
            "filters": {
                "by_scope": dict(runtime_status.get("filter_by_scope", {}) or {}),
                "affinity_gates_skipped": int(
                    runtime_status.get("affinity_gates_skipped", 0) or 0
                ),
            },
            "warmup": {
                "ready": bool(runtime_status.get("warmup_ready", False)),
                "skipped": int(runtime_status.get("warmup_skipped", 0) or 0),
                "realtime_symbols": int(
                    runtime_status.get("warmup_realtime_symbols", 0) or 0
                ),
            },
            "active_states": {
                "preview": int(runtime_status.get("active_preview_states", 0) or 0),
                "confirmed": int(
                    runtime_status.get("active_confirmed_states", 0) or 0
                ),
            },
            "voting_groups": list(runtime_status.get("voting_groups", []) or []),
            "regime_map": dict(runtime_status.get("regime_map", {}) or {}),
            "last_run_at": runtime_status.get("last_run_at"),
            "last_error": last_error,
        }

    @classmethod
    def build_position_manager_summary(
        cls,
        manager_status: Mapping[str, Any],
        positions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        running = bool(manager_status.get("running", False))
        last_error = manager_status.get("last_error")
        return {
            "status": (
                "critical"
                if not running
                else "warning"
                if last_error
                else "healthy"
            ),
            "running": running,
            "tracked_positions": int(
                manager_status.get("tracked_positions", len(positions)) or 0
            ),
            "reconcile": {
                "interval": manager_status.get("reconcile_interval"),
                "count": int(manager_status.get("reconcile_count", 0) or 0),
                "last_at": manager_status.get("last_reconcile_at"),
            },
            "last_error": last_error,
            "config": dict(manager_status.get("config", {}) or {}),
            "last_end_of_day_close_date": manager_status.get(
                "last_end_of_day_close_date"
            ),
            "margin_guard": manager_status.get("margin_guard"),
            "positions": cls.build_positions_overview(positions),
        }

    @staticmethod
    def _safe_call(factory: Any, fallback: dict[str, Any]) -> dict[str, Any]:
        try:
            value = factory()
        except Exception:
            return dict(fallback)

        if isinstance(value, dict):
            return value
        if value is None:
            return dict(fallback)
        return {"value": value}

    def storage_summary(self) -> dict[str, Any]:
        if self._ingestor is None:
            return {
                "status": "critical",
                "threads": {},
                "summary": {},
                "worst_queue": None,
            }
        return self.build_storage_summary(self._ingestor.queue_stats())

    def indicator_summary(self) -> dict[str, Any]:
        if self._indicator_manager is None:
            return {
                "status": "critical",
                "mode": None,
                "event_loop_running": False,
                "computations": {},
                "events": {},
                "cache": {},
                "results": {},
                "config": {},
                "timestamp": None,
            }
        return self.build_indicator_summary(
            self._indicator_manager.get_performance_stats()
        )

    def trading_summary(self, *, hours: int = 24) -> dict[str, Any]:
        if self._trading_service is None:
            return {
                "status": "critical",
                "active_account_alias": None,
                "accounts": [],
                "daily": {},
                "risk": {},
                "coordination_issues": [],
                "summary": [],
                "recent": [],
            }
        return self.build_trading_summary(
            self._trading_service.monitoring_summary(hours=hours)
        )

    def health_report(self, *, hours: int = 24) -> dict[str, Any]:
        report = (
            self._health_monitor.generate_report(hours)
            if self._health_monitor is not None
            else {"status": "unavailable", "alerts": [], "metrics": {}}
        )
        report["runtime"] = {
            "storage": self.storage_summary(),
            "indicators": self.indicator_summary(),
            "trading": self.trading_summary(hours=hours),
        }
        return report

    def signal_runtime_summary(self) -> dict[str, Any]:
        if self._signal_runtime is None:
            return {
                "status": "critical",
                "running": False,
                "target_count": 0,
                "trigger_mode": {},
                "strategy_sessions": {},
                "strategy_scopes": {},
                "market_structure": {},
                "queues": {},
                "processing": {},
                "filters": {},
                "warmup": {},
                "active_states": {},
                "voting_groups": [],
                "regime_map": {},
                "last_run_at": None,
                "last_error": "signal_runtime_unavailable",
            }
        return self.build_signal_runtime_summary(self._signal_runtime.status())

    def pending_entries_summary(self) -> dict[str, Any]:
        if self._pending_entry_manager is None:
            return {
                "status": "critical",
                "active_count": 0,
                "entries": [],
                "stats": {},
            }
        return self.build_pending_entries_summary(self._pending_entry_manager.status())

    def trade_executor_summary(self) -> dict[str, Any]:
        if self._trade_executor is None:
            return {
                "status": "critical",
                "enabled": False,
                "circuit_open": False,
                "consecutive_failures": 0,
                "execution_count": 0,
                "last_execution_at": None,
                "last_error": "trade_executor_unavailable",
                "last_risk_block": None,
                "signals": {},
                "execution_quality": {},
                "config": {},
                "execution_gate": {},
                "pending_entries_count": 0,
                "pending_entries": self.pending_entries_summary(),
                "recent_executions": [],
            }
        return self.build_executor_snapshot(self._trade_executor.status())

    def position_manager_summary(self) -> dict[str, Any]:
        if self._position_manager is None:
            return {
                "status": "critical",
                "running": False,
                "tracked_positions": 0,
                "reconcile": {},
                "last_error": "position_manager_unavailable",
                "config": {},
                "last_end_of_day_close_date": None,
                "margin_guard": None,
                "positions": {"count": 0, "items": []},
            }
        positions = list(self._position_manager.active_positions())
        return self.build_position_manager_summary(
            self._position_manager.status(),
            positions,
        )

    def tracked_positions_payload(self, *, limit: int = 20) -> dict[str, Any]:
        summary = self.position_manager_summary()
        positions = list(summary.get("positions", {}).get("items", []) or [])[:limit]
        return {
            "status": summary.get("status", "critical"),
            "count": len(positions),
            "items": positions,
            "manager": {
                key: value for key, value in summary.items() if key != "positions"
            },
        }

    @staticmethod
    def _state_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            status = str(row.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts

    def pending_order_state_payload(
        self,
        *,
        statuses: Optional[list[str]] = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if self._trading_state_store is None:
            return {"count": 0, "status_counts": {}, "items": []}
        items = list(
            self._trading_state_store.list_pending_order_states(
                statuses=statuses,
                limit=limit,
            )
        )
        return {
            "count": len(items),
            "status_counts": self._state_counts(items),
            "items": items,
        }

    def active_pending_order_payload(
        self,
        *,
        limit: int = 20,
        include_orphan: bool = True,
    ) -> dict[str, Any]:
        active_statuses = ["placed"]
        if include_orphan:
            active_statuses.append("orphan")
        payload = self.pending_order_state_payload(
            statuses=active_statuses,
            limit=limit,
        )
        payload["view"] = "active"
        payload["active_statuses"] = active_statuses
        return payload

    def pending_order_lifecycle_payload(
        self,
        *,
        limit: int = 20,
    ) -> dict[str, Any]:
        payload = self.pending_order_state_payload(limit=limit)
        payload["view"] = "lifecycle"
        return payload

    def pending_execution_context_payload(self) -> dict[str, Any]:
        if self._pending_entry_manager is None:
            return {"count": 0, "source_counts": {}, "items": []}
        contexts_fn = getattr(self._pending_entry_manager, "active_execution_contexts", None)
        if callable(contexts_fn):
            items = list(contexts_fn() or [])
        else:
            status = self.pending_entries_summary()
            items = list(status.get("entries", []) or [])
        source_counts: dict[str, int] = {}
        for row in items:
            source = str(row.get("source") or "unknown")
            source_counts[source] = source_counts.get(source, 0) + 1
        return {
            "count": len(items),
            "source_counts": source_counts,
            "items": items,
            "view": "execution_contexts",
        }

    def position_runtime_state_payload(
        self,
        *,
        statuses: Optional[list[str]] = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if self._trading_state_store is None:
            return {"count": 0, "status_counts": {}, "items": []}
        items = list(
            self._trading_state_store.list_position_runtime_states(
                statuses=statuses,
                limit=limit,
            )
        )
        return {
            "count": len(items),
            "status_counts": self._state_counts(items),
            "items": items,
        }

    def persisted_trade_control_payload(self) -> dict[str, Any] | None:
        if self._trading_state_store is None:
            return None
        return self._trading_state_store.load_trade_control_state()

    def trading_state_summary(
        self,
        *,
        pending_limit: int = 20,
        position_limit: int = 20,
    ) -> dict[str, Any]:
        active_pending = self.active_pending_order_payload(limit=pending_limit)
        lifecycle_pending = self.pending_order_lifecycle_payload(limit=pending_limit)
        return {
            "trade_control": self.persisted_trade_control_payload(),
            "pending": {
                "active": active_pending,
                "lifecycle": lifecycle_pending,
                "execution_contexts": self.pending_execution_context_payload(),
            },
            "positions": self.position_runtime_state_payload(limit=position_limit),
            "alerts": self.trading_state_alerts_summary(),
        }

    def trading_state_alerts_summary(self) -> dict[str, Any]:
        if self._trading_state_alerts is None:
            return {"status": "unavailable", "alerts": [], "summary": [], "observed": {}}
        return self._trading_state_alerts.summary()

    def dashboard_overview(self, startup_status: Mapping[str, Any]) -> dict[str, Any]:
        account = self._safe_call(
            lambda: self._trading_service.health(),
            {"error": "unavailable"},
        )
        storage = self._safe_call(
            lambda: self._ingestor.queue_stats(),
            {"error": "unavailable"},
        )
        indicators = self._safe_call(
            lambda: self._indicator_manager.get_performance_stats(),
            {"error": "unavailable"},
        )

        return {
            "system": self.build_system_status(startup_status),
            "account": account,
            "positions": self.tracked_positions_payload(),
            "trading_state": self.trading_state_summary(),
            "signals": self.signal_runtime_summary(),
            "executor": self.trade_executor_summary(),
            "storage": storage,
            "indicators": indicators,
        }
