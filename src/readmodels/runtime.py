from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from src.monitoring.pipeline.events import (
    PIPELINE_ADMISSION_REPORT_APPENDED,
    PIPELINE_COMMAND_CLAIMED,
    PIPELINE_COMMAND_COMPLETED,
    PIPELINE_COMMAND_FAILED,
    PIPELINE_COMMAND_SUBMITTED,
    PIPELINE_INTENT_CLAIMED,
    PIPELINE_INTENT_DEAD_LETTERED,
    PIPELINE_INTENT_PUBLISHED,
    PIPELINE_INTENT_RECLAIMED,
    PIPELINE_RISK_STATE_CHANGED,
    PIPELINE_UNMANAGED_POSITION_DETECTED,
)
from src.trading.broker.comment_codec import looks_like_system_trade_comment


class RuntimeReadModel:
    """统一运行时读模型，供 admin、monitoring 与前端投影视图复用。"""

    def __init__(
        self,
        *,
        health_monitor: Any = None,
        market_service: Any = None,
        storage_writer: Any = None,
        ingestor: Any = None,
        indicator_manager: Any = None,
        trading_queries: Any = None,
        signal_runtime: Any = None,
        trade_executor: Any = None,
        position_manager: Any = None,
        pending_entry_manager: Any = None,
        trading_state_store: Any = None,
        trading_state_alerts: Any = None,
        exposure_closeout_controller: Any = None,
        runtime_mode_controller: Any = None,
        runtime_identity: Any = None,
        paper_trading_bridge: Any = None,
        db_writer: Any = None,
    ) -> None:
        self._health_monitor = health_monitor
        self._market_service = market_service
        self._storage_writer = storage_writer
        self._ingestor = ingestor
        self._indicator_manager = indicator_manager
        self._trading_queries = trading_queries
        self._signal_runtime = signal_runtime
        self._trade_executor = trade_executor
        self._position_manager = position_manager
        self._pending_entry_manager = pending_entry_manager
        self._trading_state_store = trading_state_store
        self._trading_state_alerts = trading_state_alerts
        self._exposure_closeout_controller = exposure_closeout_controller
        self._runtime_mode_controller = runtime_mode_controller
        self._runtime_identity = runtime_identity
        self._paper_trading_bridge = paper_trading_bridge
        self.db_writer = db_writer

    @property
    def runtime_identity(self) -> Any:
        return self._runtime_identity

    def _is_executor(self) -> bool:
        identity = self._runtime_identity
        return bool(identity is not None and identity.instance_role == "executor")

    def _runtime_identity_attr(self, attr: str, default: Any = None) -> Any:
        identity = self._runtime_identity
        if identity is None:
            return default
        return getattr(identity, attr, default)

    def _is_shared_compute_main(self) -> bool:
        return bool(
            self._runtime_identity is not None
            and self._runtime_identity_attr("instance_role") == "main"
            and self._runtime_identity_attr("live_topology_mode") == "multi_account"
        )

    def _shared_compute_main_without_local_execution(self) -> bool:
        return bool(
            self._is_shared_compute_main()
            and self._trade_executor is None
            and self._position_manager is None
            and self._pending_entry_manager is None
        )

    def _current_runtime_mode(self) -> str | None:
        controller = self._runtime_mode_controller
        if controller is None:
            return None
        try:
            snapshot = controller.snapshot()
        except Exception:
            return None
        mode = snapshot.get("current_mode")
        return str(mode) if mode else None

    @staticmethod
    def _json_safe_value(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        if isinstance(value, Mapping):
            return {
                str(key): RuntimeReadModel._json_safe_value(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [RuntimeReadModel._json_safe_value(item) for item in value]
        return value

    @staticmethod
    def _component_running(component: Any, *, fallback: bool = False) -> bool:
        if component is None:
            return fallback
        is_running = getattr(component, "is_running", None)
        if not callable(is_running):
            return fallback
        try:
            return bool(is_running())
        except Exception:
            return fallback

    @classmethod
    def _position_dict(cls, item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            payload = dict(item)
        else:
            payload = dict(getattr(item, "__dict__", {}) or {})
        if hasattr(payload.get("time"), "isoformat"):
            payload["time"] = payload["time"].isoformat()
        return cls._json_safe_value(payload)

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
        intrabar_synthesis = dict(queue_stats.get("intrabar_synthesis", {}) or {})
        worst_queue = None
        writer_alive = threads.get("writer_alive")
        ingest_alive = threads.get("ingest_alive")

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

        if writer_alive is not True:
            status = "critical"
        elif ingest_alive is False:
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
            "intrabar_synthesis": RuntimeReadModel._build_intrabar_synthesis_summary(
                intrabar_synthesis
            ),
        }

    @staticmethod
    def _build_intrabar_synthesis_summary(
        entries: Mapping[str, Any],
    ) -> dict[str, Any]:
        items = {
            str(key): dict(value)
            for key, value in dict(entries or {}).items()
            if isinstance(value, Mapping)
        }
        if not items:
            return {
                "configured": False,
                "status": "unavailable",
                "total": 0,
                "stale": 0,
                "healthy": 0,
                "warming_up": 0,
                "worst_age_seconds": None,
                "items": {},
            }
        stale_count = sum(1 for item in items.values() if bool(item.get("stale", False)))
        warming_up_count = sum(
            1 for item in items.values() if str(item.get("status") or "").strip() == "warming_up"
        )
        ages = [
            float(item.get("last_age_seconds"))
            for item in items.values()
            if isinstance(item.get("last_age_seconds"), (int, float))
            and float(item.get("last_age_seconds")) >= 0.0
        ]
        if stale_count > 0:
            status = "warning"
        elif warming_up_count > 0:
            status = "warming_up"
        else:
            status = "healthy"
        return {
            "configured": True,
            "status": status,
            "total": len(items),
            "stale": stale_count,
            "healthy": max(len(items) - stale_count - warming_up_count, 0),
            "warming_up": warming_up_count,
            "worst_age_seconds": max(ages) if ages else None,
            "items": items,
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
                "intrabar_stale_drops": int(
                    runtime_status.get("dropped_intrabar_stale", 0) or 0
                ),
                "dropped_events": dropped_events,
                "dropped_confirmed": int(
                    runtime_status.get("dropped_confirmed", 0) or 0
                ),
                "dropped_intrabar": int(
                    runtime_status.get("dropped_intrabar", 0) or 0
                ),
            },
            "drop_rates": dict(runtime_status.get("drop_rates", {}) or {}),
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
                "confirmed": int(
                    runtime_status.get("active_confirmed_states", 0) or 0
                ),
            },
            "regime_map": dict(runtime_status.get("regime_map", {}) or {}),
            "last_run_at": runtime_status.get("last_run_at"),
            "last_error": last_error,
            "intrabar_trade_coordinator": (
                runtime_status.get("intrabar_trade_coordinator")
            ),
            "intrabar_runtime_slos": dict(
                runtime_status.get("intrabar_runtime_slos", {}) or {}
            ),
            "strategy_capability_reconciliation": dict(
                runtime_status.get("strategy_capability_reconciliation", {})
            ),
            "strategy_capability_execution_plan": dict(
                runtime_status.get("strategy_capability_execution_plan", {})
            ),
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
        if self._is_executor():
            writer_alive = bool(
                self._storage_writer is not None
                and getattr(self._storage_writer, "is_running", lambda: False)()
            )
            queue_stats = (
                self._storage_writer.stats()
                if self._storage_writer is not None
                and hasattr(self._storage_writer, "stats")
                else {"queues": {}, "summary": {}, "threads": {}}
            )
            sanitized_stats = {
                **dict(queue_stats or {}),
                "threads": {
                    **dict((queue_stats or {}).get("threads", {}) or {}),
                    "writer_alive": writer_alive,
                    # executor 没有共享 ingestion 线程，这里只用于复用 worst_queue 计算，
                    # 真正状态判定会在后面单独覆盖。
                    "ingest_alive": True,
                },
            }
            summary = self.build_storage_summary(sanitized_stats)
            threads = dict(summary.get("threads", {}) or {})
            threads["writer_alive"] = writer_alive
            threads["ingest_alive"] = False
            queue_totals = dict(summary.get("summary", {}) or {})
            if writer_alive is not True:
                summary["status"] = "critical"
            elif int(queue_totals.get("full", 0) or 0) > 0 or int(
                queue_totals.get("critical", 0) or 0
            ) > 0:
                summary["status"] = "critical"
            elif int(queue_totals.get("high", 0) or 0) > 0:
                summary["status"] = "warning"
            else:
                summary["status"] = "healthy"
            summary["threads"] = threads
            summary["ingestion"] = "disabled"
            summary["role"] = "executor"
            return summary
        if self._ingestor is None:
            return {
                "status": "critical",
                "threads": {},
                "summary": {},
                "worst_queue": None,
            }
        summary = self.build_storage_summary(self._ingestor.queue_stats())
        return summary

    def indicator_summary(self) -> dict[str, Any]:
        if self._is_executor():
            return {
                "status": "disabled",
                "mode": None,
                "event_loop_running": False,
                "computations": {},
                "events": {},
                "cache": {},
                "results": {},
                "config": {},
                "timestamp": None,
                "role": "executor",
            }
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
        summary = self.build_indicator_summary(
            self._indicator_manager.get_performance_stats()
        )
        if (
            self._current_runtime_mode() in {"risk_off", "ingest_only"}
            and not summary.get("event_loop_running", False)
        ):
            summary["status"] = "disabled"
        return summary

    def trading_summary(self, *, hours: int = 24) -> dict[str, Any]:
        if self._trading_queries is None:
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
            self._trading_queries.monitoring_summary(hours=hours)
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
            "external_dependencies": {
                "mt5_session": self.mt5_session_summary(),
            },
        }
        return report

    def signal_runtime_summary(self) -> dict[str, Any]:
        if self._is_executor():
            executor_summary = self.trade_executor_summary()
            return {
                "status": "disabled",
                "running": False,
                "target_count": 0,
                "trigger_mode": {},
                "strategy_sessions": {},
                "strategy_scopes": {},
                "market_structure": {},
                "queues": {},
                "processing": {},
                "filters": {},
                "strategy_capability_reconciliation": {
                    "reconciled": False,
                    "module_count": 0,
                    "runtime_count": 0,
                    "module_only": [],
                    "runtime_only": [],
                    "drift_items": [],
                    "drift_count": 0,
                },
                "strategy_capability_execution_plan": {
                    "configured_target_count": 0,
                    "scheduled_target_count": 0,
                    "filtered_target_count": 0,
                    "strategy_capability_count": 0,
                    "configured_strategies": [],
                    "scheduled_strategies": [],
                    "filtered_strategies": [],
                    "scope_strategies": {"confirmed": [], "intrabar": []},
                    "needs_intrabar_strategies": [],
                    "needs_htf_strategies": [],
                    "required_indicators_by_strategy": {},
                    "required_indicators_union": [],
                    "strategy_timeframes_policy": {},
                    "filtered_reason_counts": {},
                    "filtered_target_samples": [],
                    "filtered_target_sample_overflow": 0,
                },
                "warmup": {},
                "active_states": {},
                "executor_enabled": bool(executor_summary.get("enabled", False)),
                "execution_gate": dict(executor_summary.get("execution_gate", {}) or {}),
                "active_filters": [],
                "filter_stats": {
                    "configured": {},
                    "totals": {},
                    "window": {},
                    "window_seconds": 0,
                    "window_elapsed": 0,
                },
                "regime_map": {},
                "last_run_at": None,
                "last_error": None,
                "role": "executor",
            }
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
                "strategy_capability_reconciliation": {
                    "reconciled": False,
                    "module_count": 0,
                    "runtime_count": 0,
                    "module_only": [],
                    "runtime_only": [],
                    "drift_items": [],
                    "drift_count": 0,
                },
                "strategy_capability_execution_plan": {
                    "configured_target_count": 0,
                    "scheduled_target_count": 0,
                    "filtered_target_count": 0,
                    "strategy_capability_count": 0,
                    "configured_strategies": [],
                    "scheduled_strategies": [],
                    "filtered_strategies": [],
                    "scope_strategies": {"confirmed": [], "intrabar": []},
                    "needs_intrabar_strategies": [],
                    "needs_htf_strategies": [],
                    "required_indicators_by_strategy": {},
                    "required_indicators_union": [],
                    "strategy_timeframes_policy": {},
                    "filtered_reason_counts": {},
                    "filtered_target_samples": [],
                    "filtered_target_sample_overflow": 0,
                },
                "warmup": {},
                "active_states": {},
                "executor_enabled": False,
                "execution_gate": {},
                "active_filters": [],
                "filter_stats": {
                    "configured": {},
                    "totals": {},
                    "window": {},
                    "window_seconds": 0,
                    "window_elapsed": 0,
                },
                "regime_map": {},
                "last_run_at": None,
                "last_error": "signal_runtime_unavailable",
            }
        runtime_status = self._signal_runtime.status()
        summary = self.build_signal_runtime_summary(runtime_status)
        executor_summary = self.trade_executor_summary()
        filter_realtime_status = dict(
            runtime_status.get("filter_realtime_status", {}) or {}
        )
        summary["executor_enabled"] = bool(executor_summary.get("enabled", False))
        summary["execution_gate"] = dict(executor_summary.get("execution_gate", {}) or {})
        summary["active_filters"] = sorted(filter_realtime_status.keys())
        summary["filter_stats"] = {
            "configured": filter_realtime_status,
            "totals": dict(runtime_status.get("filter_by_scope", {}) or {}),
            "window": dict(runtime_status.get("filter_window_by_scope", {}) or {}),
            "window_seconds": int(runtime_status.get("filter_window_seconds", 0) or 0),
            "window_elapsed": int(runtime_status.get("filter_window_elapsed", 0) or 0),
        }
        if (
            self._current_runtime_mode() in {"risk_off", "ingest_only"}
            and not summary.get("running", False)
        ):
            summary["status"] = "disabled"
        return summary

    def pending_entries_summary(self) -> dict[str, Any]:
        if self._pending_entry_manager is None:
            if self._shared_compute_main_without_local_execution():
                # multi_account main 不本地执行，委托给 workers。
                # status="disabled" 保留是为了兼容旧前端；state="delegated" 明确语义，
                # 告诉 dashboard/监控这不是故障而是拓扑正确行为。
                return {
                    "status": "disabled",
                    "state": "delegated",
                    "configured": False,
                    "running": False,
                    "active_count": 0,
                    "entries": [],
                    "stats": {},
                    "execution_scope": "remote_executor",
                }
            return {
                "status": "critical",
                "configured": False,
                "running": False,
                "active_count": 0,
                "entries": [],
                "stats": {},
            }
        summary = self.build_pending_entries_summary(self._pending_entry_manager.status())
        summary["configured"] = True
        summary["running"] = self._component_running(self._pending_entry_manager)
        summary.setdefault("execution_scope", "local")
        if self._current_runtime_mode() == "ingest_only":
            summary["status"] = "disabled"
            summary["running"] = False
        return summary

    def trade_executor_summary(self) -> dict[str, Any]:
        if self._trade_executor is None:
            if self._shared_compute_main_without_local_execution():
                # 见 pending_entries_summary 注释
                return {
                    "status": "disabled",
                    "state": "delegated",
                    "configured": False,
                    "armed": False,
                    "running": False,
                    "enabled": False,
                    "circuit_open": False,
                    "consecutive_failures": 0,
                    "execution_count": 0,
                    "last_execution_at": None,
                    "last_error": None,
                    "last_risk_block": None,
                    "signals": {},
                    "execution_quality": {},
                    "config": {},
                    "execution_gate": {},
                    "pending_entries_count": 0,
                    "pending_entries": self.pending_entries_summary(),
                    "recent_executions": [],
                    "execution_scope": "remote_executor",
                }
            return {
                "status": "critical",
                "configured": False,
                "armed": False,
                "running": False,
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
        summary = self.build_executor_snapshot(self._trade_executor.status())
        summary["configured"] = True
        summary["armed"] = bool(summary.get("enabled", False))
        summary["running"] = self._component_running(
            self._trade_executor,
            fallback=summary["armed"],
        )
        summary.setdefault("execution_scope", "local")
        return summary

    def position_manager_summary(self) -> dict[str, Any]:
        if self._position_manager is None:
            if self._shared_compute_main_without_local_execution():
                # 见 pending_entries_summary 注释
                return {
                    "status": "disabled",
                    "state": "delegated",
                    "configured": False,
                    "running": False,
                    "tracked_positions": 0,
                    "reconcile": {},
                    "last_error": None,
                    "config": {},
                    "last_end_of_day_close_date": None,
                    "margin_guard": None,
                    "positions": {"count": 0, "items": []},
                    "execution_scope": "remote_executor",
                }
            return {
                "status": "critical",
                "configured": False,
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
        summary = self.build_position_manager_summary(
            self._position_manager.status(),
            positions,
        )
        summary["configured"] = True
        summary.setdefault("execution_scope", "local")
        if (
            self._current_runtime_mode() == "ingest_only"
            and not summary.get("running", False)
        ):
            summary["status"] = "disabled"
        return summary

    def runtime_mode_summary(self) -> dict[str, Any]:
        controller = self._runtime_mode_controller
        if controller is None:
            return {"status": "unavailable", "current_mode": None}
        snapshot = dict(controller.snapshot() or {})
        snapshot["status"] = "healthy"
        snapshot["validation_sidecars"] = {
            "paper_trading": self.paper_trading_summary()
        }
        return snapshot

    def paper_trading_summary(self) -> dict[str, Any]:
        bridge = self._paper_trading_bridge
        if bridge is None:
            return {
                "kind": "validation_sidecar",
                "configured": False,
                "running": False,
                "status": "disabled",
                "session_id": None,
                "signals_received": 0,
                "signals_executed": 0,
                "signals_rejected": 0,
                "reject_reasons": {},
                "active_symbols": [],
            }

        try:
            payload = dict(bridge.status() or {})
        except Exception as exc:
            return {
                "kind": "validation_sidecar",
                "configured": True,
                "running": False,
                "status": "warning",
                "last_error": str(exc),
                "session_id": None,
                "signals_received": 0,
                "signals_executed": 0,
                "signals_rejected": 0,
                "reject_reasons": {},
                "active_symbols": [],
            }

        running = bool(payload.get("running", False))
        session_id = payload.get("session_id") or payload.get("session")
        configured = True
        status = "healthy" if running else "idle"
        return {
            "kind": "validation_sidecar",
            "configured": configured,
            "running": running,
            "status": status,
            "session_id": session_id,
            "started_at": payload.get("started_at"),
            "signals_received": int(payload.get("signals_received", 0) or 0),
            "signals_executed": int(payload.get("signals_executed", 0) or 0),
            "signals_rejected": int(payload.get("signals_rejected", 0) or 0),
            "reject_reasons": dict(payload.get("reject_reasons", {}) or {}),
            "active_symbols": list(payload.get("active_symbols", []) or []),
            "current_balance": payload.get("current_balance"),
            "floating_pnl": payload.get("floating_pnl"),
            "equity": payload.get("equity"),
            "open_positions": payload.get("open_positions"),
            "closed_trades": payload.get("closed_trades"),
        }

    def mt5_session_summary(self) -> dict[str, Any]:
        market_service = self._market_service
        if market_service is None:
            return {
                "kind": "external_dependency",
                "status": "unavailable",
                "connected": False,
                "terminal_reachable": False,
                "terminal_process_ready": False,
                "ipc_ready": False,
                "authorized": False,
                "account_match": False,
                "session_ready": False,
                "interactive_login_required": False,
                "error_code": "market_service_unavailable",
                "error_message": "market service is not configured",
                "last_error": {"code": None, "message": None},
            }

        client = getattr(market_service, "client", None)
        inspect = getattr(client, "inspect_session_state", None)
        if callable(inspect):
            try:
                state = inspect(
                    require_terminal_process=True,
                    attempt_initialize=True,
                    attempt_login=True,
                )
                payload = (
                    state.to_dict()
                    if hasattr(state, "to_dict")
                    else dict(state or {})
                )
                return {
                    "kind": "external_dependency",
                    "status": "healthy" if bool(payload.get("session_ready")) else "critical",
                    "connected": bool(payload.get("session_ready")),
                    **payload,
                }
            except Exception as exc:
                return {
                    "kind": "external_dependency",
                    "status": "critical",
                    "connected": False,
                    "terminal_reachable": False,
                    "terminal_process_ready": False,
                    "ipc_ready": False,
                    "authorized": False,
                    "account_match": False,
                    "session_ready": False,
                    "interactive_login_required": False,
                    "error_code": "probe_failed",
                    "error_message": str(exc),
                    "last_error": {"code": None, "message": None},
                }

        return {
            "kind": "external_dependency",
            "status": "unavailable",
            "connected": False,
            "terminal_reachable": False,
            "terminal_process_ready": False,
            "ipc_ready": False,
            "authorized": False,
            "account_match": False,
            "session_ready": False,
            "interactive_login_required": False,
            "error_code": "mt5_client_unavailable",
            "error_message": "market service does not expose MT5 session inspection",
            "last_error": {"code": None, "message": None},
        }

    def exposure_closeout_summary(self) -> dict[str, Any]:
        controller = self._exposure_closeout_controller
        if controller is None:
            return {
                "status": "unavailable",
                "last_reason": None,
                "last_comment": None,
                "last_requested_at": None,
                "last_completed_at": None,
                "result": None,
            }
        snapshot = dict(controller.status() or {})
        snapshot.setdefault("status", "idle")
        return snapshot

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
        payload = self._trading_state_store.load_trade_control_state()
        if not isinstance(payload, dict):
            return payload
        return self._json_safe_value(payload)

    def account_risk_state_summary(self) -> dict[str, Any] | None:
        if self._trading_state_store is not None:
            try:
                state = self._trading_state_store.load_account_risk_state()
            except Exception:
                state = None
            if isinstance(state, dict) and state:
                return state
        if self.db_writer is None or self._runtime_identity is None:
            return None
        try:
            return self.db_writer.fetch_account_risk_state(
                account_key=self._runtime_identity.account_key,
            )
        except Exception:
            return None

    def account_risk_states_payload(self, *, limit: int = 100) -> dict[str, Any]:
        if self.db_writer is None:
            return {"count": 0, "items": []}
        try:
            items = list(self.db_writer.fetch_account_risk_states(limit=limit) or [])
        except Exception:
            return {"count": 0, "items": []}
        return {"count": len(items), "items": items}

    def trade_control_summary(self) -> dict[str, Any] | None:
        if self._trading_queries is not None:
            try:
                state = self._trading_queries.trade_control_status()
            except Exception:
                state = None
            if isinstance(state, dict) and state:
                return state
        return self.persisted_trade_control_payload()

    def _live_positions_payload(self) -> list[dict[str, Any]]:
        if self._trading_queries is None:
            return []
        try:
            rows = list(self._trading_queries.get_positions(None, None) or [])
        except Exception:
            return []
        return [self._position_dict(item) for item in rows]

    def unmanaged_live_positions_payload(self, *, limit: int = 20) -> dict[str, Any]:
        live_positions = self._live_positions_payload()
        managed_items = list(
            self.position_runtime_state_payload(statuses=["open"], limit=max(limit * 5, 100)).get("items")
            or []
        )
        managed_tickets = {
            int(ticket)
            for ticket in (
                item.get("position_ticket")
                for item in managed_items
            )
            if ticket is not None
        }
        context_resolver = (
            getattr(self._trading_queries, "resolve_position_context", None)
            if self._trading_queries is not None
            else None
        )
        unmanaged_items: list[dict[str, Any]] = []
        for position in live_positions:
            ticket = position.get("ticket")
            try:
                normalized_ticket = int(ticket)
            except (TypeError, ValueError):
                normalized_ticket = None
            if normalized_ticket is not None and normalized_ticket in managed_tickets:
                continue
            comment = str(position.get("comment") or "").strip()
            magic = position.get("magic")
            context = None
            if callable(context_resolver) and normalized_ticket is not None:
                try:
                    context = context_resolver(ticket=normalized_ticket, comment=comment, limit=200)
                except Exception:
                    context = None
            if not comment and int(magic or 0) == 0:
                reason = "manual_position"
            elif context is None and not looks_like_system_trade_comment(comment):
                reason = "unsupported_comment"
            else:
                reason = "missing_context"
            unmanaged_items.append(
                {
                    "ticket": normalized_ticket,
                    "symbol": position.get("symbol"),
                    "volume": position.get("volume"),
                    "type": position.get("type"),
                    "magic": magic,
                    "comment": comment,
                    "reason": reason,
                    "context": context,
                }
            )
        reason_counts: dict[str, int] = {}
        for item in unmanaged_items:
            reason = str(item.get("reason") or "unknown")
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        return {
            "count": len(unmanaged_items),
            "managed_count": len(managed_tickets),
            "live_count": len(live_positions),
            "reason_counts": reason_counts,
            "items": unmanaged_items[:limit],
        }

    def tradability_state_summary(self) -> dict[str, Any]:
        account_risk = dict(self.account_risk_state_summary() or {})
        trade_control = dict(self.trade_control_summary() or {})
        runtime_mode = dict(self.runtime_mode_summary() or {})
        quote_health = dict(account_risk.get("metadata", {}).get("quote_health", {}) or {})
        margin_guard = dict(account_risk.get("metadata", {}).get("margin_guard", {}) or {})
        auto_entry_enabled = bool(account_risk.get("auto_entry_enabled", trade_control.get("auto_entry_enabled", True)))
        close_only_mode = bool(account_risk.get("close_only_mode", trade_control.get("close_only_mode", False)))
        circuit_open = bool(account_risk.get("circuit_open", False))
        quote_stale = bool(account_risk.get("quote_stale", False))
        should_block_new_trades = bool(account_risk.get("should_block_new_trades", False))
        runtime_present = self._trade_executor is not None
        admission_enabled = bool(
            runtime_present
            and auto_entry_enabled
            and not close_only_mode
            and str(runtime_mode.get("current_mode") or "full") == "full"
        )
        return {
            "runtime_present": runtime_present,
            "admission_enabled": admission_enabled,
            "market_data_fresh": not quote_stale,
            "quote_health": {
                "stale": quote_stale,
                "age_seconds": quote_health.get("age_seconds"),
                "stale_threshold_seconds": quote_health.get("stale_threshold_seconds"),
            },
            "session_allowed": {
                "status": "unknown",
                "reason": None,
            },
            "economic_guard": {
                "status": "warn_only",
                "degraded": bool(account_risk.get("indicator_degraded", False) or account_risk.get("db_degraded", False)),
            },
            "auto_entry_enabled": auto_entry_enabled,
            "close_only_mode": close_only_mode,
            "margin_guard": margin_guard,
            "circuit_open": circuit_open,
            "tradable": bool(
                admission_enabled
                and not circuit_open
                and not should_block_new_trades
                and not quote_stale
            ),
        }

    def recent_trade_pipeline_events_payload(self, *, limit: int = 50) -> dict[str, Any]:
        if self.db_writer is None or self._runtime_identity is None:
            return {"count": 0, "items": []}
        rows = list(
            self.db_writer.fetch_pipeline_trace_filtered(
                instance_id=getattr(self._runtime_identity, "instance_id", None),
                account_key=getattr(self._runtime_identity, "account_key", None),
                event_types=[
                    PIPELINE_ADMISSION_REPORT_APPENDED,
                    PIPELINE_INTENT_PUBLISHED,
                    PIPELINE_INTENT_CLAIMED,
                    PIPELINE_INTENT_RECLAIMED,
                    PIPELINE_INTENT_DEAD_LETTERED,
                    PIPELINE_COMMAND_SUBMITTED,
                    PIPELINE_COMMAND_CLAIMED,
                    PIPELINE_COMMAND_COMPLETED,
                    PIPELINE_COMMAND_FAILED,
                    PIPELINE_RISK_STATE_CHANGED,
                    PIPELINE_UNMANAGED_POSITION_DETECTED,
                ],
                limit=limit,
                offset=0,
            )
            or []
        )
        items = [
            {
                "id": row.get("id"),
                "trace_id": row.get("trace_id"),
                "symbol": row.get("symbol"),
                "timeframe": row.get("timeframe"),
                "scope": row.get("scope"),
                "event_type": row.get("event_type"),
                "recorded_at": self._json_safe_value(row.get("recorded_at")),
                "payload": self._json_safe_value(row.get("payload") or {}),
                "instance_id": row.get("instance_id"),
                "instance_role": row.get("instance_role"),
                "account_key": row.get("account_key"),
                "signal_id": row.get("signal_id"),
                "intent_id": row.get("intent_id"),
                "command_id": row.get("command_id"),
                "action_id": row.get("action_id"),
            }
            for row in rows
        ]
        items.sort(
            key=lambda item: (
                str(item.get("recorded_at") or ""),
                int(item.get("id") or 0),
            )
        )
        return {"count": len(items), "items": items}

    def trading_state_summary(
        self,
        *,
        pending_limit: int = 20,
        position_limit: int = 20,
    ) -> dict[str, Any]:
        active_pending = self.active_pending_order_payload(limit=pending_limit)
        lifecycle_pending = self.pending_order_lifecycle_payload(limit=pending_limit)
        return {
            "trade_control": self.trade_control_summary(),
            "account_risk": self.account_risk_state_summary(),
            "tradability": self.tradability_state_summary(),
            "runtime_mode": self.runtime_mode_summary(),
            "closeout": self.exposure_closeout_summary(),
            "pending": {
                "active": active_pending,
                "lifecycle": lifecycle_pending,
                "execution_contexts": self.pending_execution_context_payload(),
            },
            "positions": self.position_runtime_state_payload(limit=position_limit),
            "managed_positions": {
                "count": int(
                    self.position_runtime_state_payload(statuses=["open"], limit=max(position_limit, 100)).get("count")
                    or 0
                ),
            },
            "unmanaged_live_positions": self.unmanaged_live_positions_payload(limit=position_limit),
            "pipeline_events": self.recent_trade_pipeline_events_payload(limit=position_limit),
            "alerts": self.trading_state_alerts_summary(),
            "validation": {
                "paper_trading": self.paper_trading_summary(),
            },
        }

    def trading_state_alerts_summary(self) -> dict[str, Any]:
        if self._trading_state_alerts is None:
            return {"status": "unavailable", "alerts": [], "summary": [], "observed": {}}
        return self._trading_state_alerts.summary()

    def dashboard_overview(self, startup_status: Mapping[str, Any]) -> dict[str, Any]:
        account = self._safe_call(
            lambda: self._trading_queries.health(),
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
            "account_risk": self.account_risk_state_summary(),
            "signals": self.signal_runtime_summary(),
            "executor": self.trade_executor_summary(),
            "validation": {"paper_trading": self.paper_trading_summary()},
            "external_dependencies": {
                "mt5_session": self.mt5_session_summary(),
            },
            "storage": storage,
            "indicators": indicators,
        }
