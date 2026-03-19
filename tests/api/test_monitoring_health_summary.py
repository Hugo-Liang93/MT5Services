from __future__ import annotations

from src.api.monitoring import (
    TRADE_TRIGGER_METHODS,
    _build_runtime_health_summary,
    _build_runtime_trading_summary,
    _build_storage_runtime_summary,
)


def test_build_runtime_health_summary_includes_event_outcomes() -> None:
    summary = _build_runtime_health_summary(
        {
            "mode": "event_driven",
            "event_loop_running": True,
            "last_reconcile_at": "2026-03-17T00:00:00+00:00",
            "total_computations": 120,
            "failed_computations": 2,
            "success_rate": 98.3,
            "cached_computations": 30,
            "incremental_computations": 40,
            "parallel_computations": 60,
            "cache_hits": 10,
            "cache_misses": 3,
            "event_store": {
                "pending": 1,
                "processing": 2,
                "completed": 100,
                "skipped": 5,
                "failed": 0,
                "retrying": 1,
                "total_retries": 4,
                "outcome_counts": {"completed": 100, "skipped_insufficient_history": 5},
                "recent_skips": [{"outcome": "skipped_insufficient_history"}],
                "recent_retryable_errors": [{"error_message": "boom"}],
                "recent_errors": [],
            },
            "pipeline": {"cache": {"hits": 10, "misses": 3}},
            "results": {"total_results": 6},
            "config": {"enabled_indicators": 6},
            "timestamp": "2026-03-17T00:00:01+00:00",
        }
    )

    assert summary["status"] == "warning"
    assert summary["computations"]["total"] == 120
    assert summary["events"]["skipped"] == 5
    assert summary["events"]["retrying"] == 1
    assert summary["events"]["outcome_counts"]["skipped_insufficient_history"] == 5
    assert summary["events"]["recent_skips"][0]["outcome"] == "skipped_insufficient_history"
    assert summary["events"]["recent_retryable_errors"][0]["error_message"] == "boom"
    assert summary["events"]["recent_errors"] == []
    assert summary["cache"]["snapshot"] == {"hits": 10, "misses": 3}


def test_build_runtime_health_summary_marks_critical_when_event_loop_stops() -> None:
    summary = _build_runtime_health_summary(
        {
            "event_loop_running": False,
            "failed_computations": 0,
            "event_store": {"failed": 0, "retrying": 0},
        }
    )

    assert summary["status"] == "critical"


def test_build_storage_runtime_summary_marks_warning_for_high_queue() -> None:
    summary = _build_storage_runtime_summary(
        {
            "threads": {"writer_alive": True, "ingest_alive": True},
            "summary": {"total": 4, "high": 1, "critical": 0, "full": 0},
            "queues": {
                "ticks": {"status": "normal", "utilization_pct": 12.0, "pending": 0},
                "ohlc": {"status": "high", "utilization_pct": 83.5, "pending": 10},
            },
        }
    )

    assert summary["status"] == "warning"
    assert summary["worst_queue"]["name"] == "ohlc"
    assert summary["worst_queue"]["status"] == "high"


def test_build_runtime_trading_summary_includes_risk_and_coordination_issues() -> None:
    summary = _build_runtime_trading_summary(
        {
            "active_account_alias": "live",
            "accounts": [{"alias": "live"}],
            "summary": [{"status": "failed", "count": 2}],
            "recent": [],
            "daily": {"failed": 2, "success": 0, "risk": {"blocked": 1, "warn": 0, "allow": 3}},
        }
    )

    assert summary["status"] == "warning"
    assert summary["risk"]["blocked"] == 1
    assert any("风控拦截" in msg for msg in summary["coordination_issues"])


def test_trade_trigger_methods_include_signal_and_trade_paths() -> None:
    method_ids = {item["id"] for item in TRADE_TRIGGER_METHODS}
    signal_method = next(item for item in TRADE_TRIGGER_METHODS if item["id"] == "signal_api_execute_trade")

    assert "trade_api_direct" in method_ids
    assert "trade_api_dispatch" in method_ids
    assert "trade_api_batch" in method_ids
    assert "signal_api_execute_trade" in method_ids
    assert "signal_runtime_auto_trade" in method_ids
    assert signal_method["path"] == "/trade/from-signal"
